import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from _common import atomic_write_text, expand_path, file_lock, nudge_disabled, nudge_stamp


HOOK_DIR = Path(__file__).resolve().parent


def repository_root():
    candidate = HOOK_DIR.parent.parent
    if (candidate / "install.py").is_file():
        return candidate
    configured = os.environ.get("HARNESS_REPO")
    if configured and (Path(configured) / "install.py").is_file():
        return Path(configured).resolve()
    candidate = Path.cwd()
    if (candidate / "install.py").is_file():
        return candidate.resolve()
    raise RuntimeError("cannot locate repository root")


REPO = repository_root()
TEST_MACHINE = os.environ.get("HARNESS_TEST_MACHINE") or sorted(
    path.stem for path in (REPO / "machines").glob("*.json")
    if not path.name.endswith(".local.json")
)[0]
BUNDLE_BUILD_TESTS = (REPO / "bundle-terms.txt").is_file()


class AtomicWriteTextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()

    def test_creates_parent_directory_and_writes_lf_only(self):
        path = self.base / "new-dir/x.txt"
        self.assertFalse(path.parent.exists())
        atomic_write_text(path, "a\nb\n")
        content = path.read_bytes()
        self.assertEqual(content, b"a\nb\n")
        self.assertNotIn(b"\r", content)

    def test_overwrites_existing_file(self):
        path = self.base / "x.txt"
        path.write_text("before", encoding="utf-8")
        atomic_write_text(path, "after")
        self.assertEqual(path.read_text(encoding="utf-8"), "after")
        self.assertEqual(list(self.base.iterdir()), [path])

    def test_failing_replace_leaves_no_temp_file(self):
        path = self.base / "outdir"
        path.mkdir()
        with self.assertRaises(OSError):
            atomic_write_text(path, "content")
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    def test_permission_error_replace_retries(self):
        path = self.base / "x.txt"
        path.write_text("before", encoding="utf-8")
        original_replace = Path.replace
        attempts = 0

        def replace(source, target):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = PermissionError("target is busy")
                error.winerror = 5
                raise error
            return original_replace(source, target)

        with mock.patch.object(Path, "replace", autospec=True, side_effect=replace), mock.patch("_common.time.sleep") as sleep:
            atomic_write_text(path, "after")
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(0.05)
        self.assertEqual(path.read_text(encoding="utf-8"), "after")
        self.assertEqual(list(self.base.iterdir()), [path])

    def test_permanent_permission_error_cleans_temporary(self):
        path = self.base / "x.txt"
        path.write_text("before", encoding="utf-8")
        error = PermissionError("target is busy")
        error.winerror = 32
        with (
            mock.patch.object(Path, "replace", side_effect=error) as replace,
            mock.patch("_common.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(PermissionError, "target is busy"):
                atomic_write_text(path, "after")
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(sleep.call_args_list, [mock.call(0.05 * attempt) for attempt in range(1, 6)])
        self.assertEqual(path.read_text(encoding="utf-8"), "before")
        self.assertEqual(list(self.base.iterdir()), [path])

    def test_other_permission_errors_do_not_retry(self):
        for winerror in (None, 13):
            with self.subTest(winerror=winerror):
                path = self.base / "x.txt"
                error = PermissionError("not retryable")
                if winerror is not None:
                    error.winerror = winerror
                with (
                    mock.patch.object(Path, "replace", side_effect=error) as replace,
                    mock.patch("_common.time.sleep") as sleep,
                ):
                    with self.assertRaises(PermissionError):
                        atomic_write_text(path, "content")
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(list(self.base.iterdir()), [])

    def test_read_only_target_does_not_retry(self):
        path = self.base / "x.txt"
        path.write_text("before", encoding="utf-8")
        path.chmod(0o444)
        try:
            with mock.patch("_common.time.sleep") as sleep:
                if os.name == "nt":
                    with self.assertRaises(PermissionError):
                        atomic_write_text(path, "after")
                else:
                    atomic_write_text(path, "after")
            sleep.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "before" if os.name == "nt" else "after")
            self.assertEqual(list(self.base.iterdir()), [path])
        finally:
            path.chmod(0o666)

    def test_read_only_parent_does_not_retry(self):
        path = self.base / "x.txt"
        if os.name == "nt":
            path.parent.chmod(0o444)
        try:
            with (
                mock.patch.object(Path, "replace", autospec=True, side_effect=Path.replace) as replace,
                mock.patch("_common.os.access", return_value=False) as access,
                mock.patch("_common.time.sleep") as sleep,
            ):
                if os.name == "nt":
                    atomic_write_text(path, "content")
                    replace.assert_called_once()
                    access.assert_not_called()
                    self.assertEqual(path.read_text(encoding="utf-8"), "content")
                else:
                    with self.assertRaisesRegex(PermissionError, "parent directory is not writable"):
                        atomic_write_text(path, "content")
                    replace.assert_not_called()
                    access.assert_called_once_with(path.parent, os.W_OK)
            sleep.assert_not_called()
            self.assertEqual(list(self.base.iterdir()), [path] if os.name == "nt" else [])
        finally:
            if os.name == "nt":
                path.parent.chmod(0o666)

    def test_retry_checks_writability_once(self):
        path = self.base / "x.txt"
        path.write_text("before", encoding="utf-8")
        error = PermissionError("target is busy")
        error.winerror = 33
        with (
            mock.patch.object(Path, "replace", side_effect=error),
            mock.patch("_common.os.access", return_value=True) as access,
            mock.patch("_common.time.sleep"),
        ):
            with self.assertRaises(PermissionError):
                atomic_write_text(path, "after")
        self.assertEqual(access.call_args_list, [] if os.name == "nt" else [mock.call(path.parent, os.W_OK)])
        self.assertEqual(list(self.base.iterdir()), [path])

    def test_sequential_writes_keep_second_content(self):
        path = self.base / "x.txt"
        atomic_write_text(path, "first")
        atomic_write_text(path, "second")
        self.assertEqual(path.read_text(encoding="utf-8"), "second")
        self.assertEqual(list(self.base.iterdir()), [path])


class FileLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "nested" / "shared.json"

    def test_shared_path_expansion(self):
        home = self.path.parent
        cases = [("~", home), ("~/x", home / "x"), ("rel", home / "rel"), (".", home)]
        if os.name == "nt":
            cases.extend([("C:transcripts", home / "transcripts"), ("Z:transcripts", home / "transcripts"),
                          ("/rooted", home / "rooted"), ("C:/abs", Path("C:/abs"))])
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expand_path(value, home), expected)
        with self.assertRaisesRegex(ValueError, "unsupported home form"):
            expand_path("~nosuchuser", home)

    def test_shared_path_rejects_empty_and_absolute_home_tails(self):
        for value in ("", "~//x", "~/\\x", "~/C:/abs"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                expand_path(value, self.path.parent)

    def test_shared_path_repo_placeholder_requires_repo(self):
        home, repo = self.path.parent, self.path.parent / "repo"
        for value, expected in (("<repo>", repo), ("<repo>/x", repo / "x"), ("<repo>\\x", repo / "x")):
            with self.subTest(value=value):
                self.assertEqual(expand_path(value, home, repo), expected)
                with self.assertRaisesRegex(ValueError, "<repo> is not available here"):
                    expand_path(value, home)

    def test_shared_path_home_tail_rejects_parent_segments(self):
        home, repo = self.path.parent, self.path.parent / "repo"
        values = ["~/../x", "~/a/../../x", "~\\a\\..\\x", "<repo>/../../secrets",
                  "<repo>/C:/Windows", "<repo>//rooted", "../../secrets", "a/../x"]
        if os.name == "nt":
            values.extend(["C:../../x", "/../../x", "\\..\\x"])
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                expand_path(value, home, repo)
        for repository in (None, repo):
            with self.subTest(repo=repository), self.assertRaisesRegex(ValueError, "invalid repository placeholder"):
                expand_path("<repo>foo", home, repository)
        for value, expected in (("~/a/./x", home / "a/x"), ("a/./x", home / "a/x"), ("<repo>/a/./x", repo / "a/x")):
            self.assertEqual(expand_path(value, home, repo), expected)

    def test_lock_creates_parent_and_retains_file(self):
        lock = self.path.with_name(self.path.name + ".lock")
        with file_lock(self.path):
            self.assertTrue(lock.is_file())
        self.assertTrue(lock.is_file())
        self.assertFalse(self.path.exists())
        with file_lock(self.path, timeout=0):
            pass

    def test_zero_timeout_attempts_once_without_sleep(self):
        with file_lock(self.path), mock.patch("_common.time.sleep") as sleep:
            with self.assertRaises(TimeoutError) as raised:
                with file_lock(self.path, timeout=0):
                    self.fail("contended lock acquired")
        self.assertEqual(str(raised.exception), f"lock on {self.path} held by another process")
        sleep.assert_not_called()

    def test_lock_releases_after_body_exception(self):
        with self.assertRaisesRegex(ValueError, "body failed"):
            with file_lock(self.path):
                raise ValueError("body failed")
        with file_lock(self.path, timeout=0):
            pass

    def test_contended_lock_retries_until_timeout(self):
        with file_lock(self.path), mock.patch("_common.time.monotonic", side_effect=[0, 0, 0.1]), mock.patch("_common.time.sleep") as sleep:
            with self.assertRaises(TimeoutError):
                with file_lock(self.path, timeout=0.1):
                    self.fail("contended lock acquired")
        sleep.assert_called_once_with(0.05)

    def test_unsupported_lock_fails_without_sleep(self):
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        error = OSError(38, "Function not implemented")
        with mock.patch(target, side_effect=error) as lock, mock.patch("_common.time.sleep") as sleep:
            with self.assertRaises(OSError) as raised:
                with file_lock(self.path):
                    self.fail("unsupported lock acquired")
        self.assertIs(raised.exception, error)
        lock.assert_called_once()
        sleep.assert_not_called()

    def test_existing_lock_parent_does_not_call_mkdir(self):
        with file_lock(self.path):
            pass
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mkdir")):
            with file_lock(self.path, timeout=0):
                pass

    @unittest.skipIf(os.name == "nt", "POSIX permission errors are not contention")
    def test_permission_error_fails_without_sleep(self):
        error = OSError(13, "Permission denied")
        with mock.patch("fcntl.flock", side_effect=error) as lock, mock.patch("_common.time.sleep") as sleep:
            with self.assertRaises(OSError) as raised:
                with file_lock(self.path):
                    self.fail("permission error acquired lock")
        self.assertIs(raised.exception, error)
        lock.assert_called_once()
        sleep.assert_not_called()

    def test_discard_keeps_lock_after_failed_unsupported_acquisition(self):
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        error = OSError(37, "No locks available")
        with mock.patch(target, side_effect=error), mock.patch("_common.time.sleep") as sleep:
            with self.assertRaises(OSError) as raised:
                with file_lock(self.path, discard=True):
                    self.fail("unsupported lock acquired")
        self.assertIs(raised.exception, error)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path.with_name(self.path.name + ".lock")])
        sleep.assert_not_called()

    def test_discard_timeout_keeps_holders_lock(self):
        lock = self.path.with_name(self.path.name + ".lock")
        with file_lock(self.path):
            with self.assertRaises(TimeoutError):
                with file_lock(self.path, timeout=0, discard=True):
                    self.fail("contended lock acquired")
            self.assertTrue(lock.exists())
            with self.assertRaises(TimeoutError):
                with file_lock(self.path, timeout=0):
                    self.fail("holder lost its lock")

    def test_discard_acquisition_failure_preserves_guarded_file_lock(self):
        self.path.parent.mkdir(parents=True)
        self.path.touch()
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        with mock.patch(target, side_effect=OSError(37, "No locks available")):
            with self.assertRaises(OSError):
                with file_lock(self.path, discard=True):
                    self.fail("unsupported lock acquired")
        self.assertTrue(self.path.with_name(self.path.name + ".lock").exists())

    def test_discard_acquisition_failure_never_attempts_unlink(self):
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        error = OSError(37, "No locks available")
        with mock.patch(target, side_effect=error), mock.patch.object(Path, "unlink") as unlink:
            with self.assertRaises(OSError) as raised:
                with file_lock(self.path, discard=True):
                    self.fail("unsupported lock acquired")
        self.assertIs(raised.exception, error)
        unlink.assert_not_called()

    def test_discard_unlink_failure_still_releases_lock(self):
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("cannot unlink")) as unlink:
            with file_lock(self.path, discard=True):
                pass
        unlink.assert_called_once_with(missing_ok=True)
        with file_lock(self.path, timeout=0):
            pass

    @unittest.skipIf(os.name == "nt", "POSIX inode validation")
    def test_discard_after_reopen_deadline_respects_contention(self):
        import errno
        import fcntl

        lock = self.path.with_name(self.path.name + ".lock")
        original_stat, original_flock = os.stat, fcntl.flock
        for failure in (None, errno.EAGAIN, errno.ENOLCK):
            calls = 0
            attempts = 0

            def replaced_stat(path, *args, **kwargs):
                nonlocal calls
                info = original_stat(path, *args, **kwargs)
                if Path(path) == lock:
                    calls += 1
                    if calls <= 4:
                        return mock.Mock(st_ino=info.st_ino + 1, st_dev=info.st_dev)
                return info

            def flock(fd, operation):
                nonlocal attempts
                if operation & fcntl.LOCK_EX:
                    attempts += 1
                    if attempts > 5:
                        self.fail("inode retry is unbounded")
                    if failure is not None and attempts == 5:
                        raise OSError(failure, "lock failed")
                return original_flock(fd, operation)

            with self.subTest(failure=failure), mock.patch("_common.os.stat", side_effect=replaced_stat), mock.patch.object(fcntl, "flock", side_effect=flock):
                with self.assertRaises(TimeoutError):
                    with file_lock(self.path, timeout=0, discard=True):
                        self.fail("replaced lock acquired")
            self.assertEqual(lock.exists(), failure is not None)

    def test_discard_removes_lock_when_guarded_file_is_absent(self):
        with file_lock(self.path, discard=True):
            self.assertTrue(self.path.with_name(self.path.name + ".lock").exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_discard_keeps_lock_when_guarded_file_exists(self):
        with file_lock(self.path, discard=True):
            self.path.touch()
        self.assertTrue(self.path.exists())
        self.assertTrue(self.path.with_name(self.path.name + ".lock").exists())

    def test_discard_runs_on_body_exception(self):
        with self.assertRaisesRegex(ValueError, "body failed"):
            with file_lock(self.path, discard=True):
                raise ValueError("body failed")
        self.assertEqual(list(self.path.parent.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "POSIX inode validation")
    def test_repeated_inode_replacement_respects_zero_timeout(self):
        lock = self.path.with_name(self.path.name + ".lock")
        original_stat = os.stat
        calls = 0

        def replaced_stat(path, *args, **kwargs):
            nonlocal calls
            info = original_stat(path, *args, **kwargs)
            if Path(path) != lock:
                return info
            calls += 1
            if calls > 5:
                self.fail("inode replacement retry is unbounded")
            return mock.Mock(st_ino=info.st_ino + calls, st_dev=info.st_dev)

        with mock.patch("_common.os.stat", side_effect=replaced_stat), mock.patch("_common.time.sleep") as sleep:
            with self.assertRaises(TimeoutError):
                with file_lock(self.path, timeout=0):
                    self.fail("replaced lock acquired")
        self.assertEqual(calls, 4)
        sleep.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX inode validation")
    def test_lock_reopens_removed_or_replaced_inode(self):
        import fcntl

        lock = self.path.with_name(self.path.name + ".lock")
        original = fcntl.flock
        for recreate in (False, True):
            acquisitions = []

            def flock(fd, operation):
                if operation & fcntl.LOCK_EX:
                    if not acquisitions:
                        lock.unlink()
                        if recreate:
                            lock.touch()
                    info = os.fstat(fd)
                    acquisitions.append((info.st_ino, info.st_dev))
                return original(fd, operation)

            with self.subTest(recreate=recreate), mock.patch.object(fcntl, "flock", side_effect=flock):
                with file_lock(self.path, timeout=0):
                    current = lock.stat()
                    self.assertEqual(len(acquisitions), 2)
                    if recreate:
                        # Without a recreate, ext4 hands the reopened file the inode it just freed, so only the recreated case guarantees a different inode.
                        self.assertNotEqual(acquisitions[0], acquisitions[1])
                    self.assertEqual(acquisitions[-1], (current.st_ino, current.st_dev))


class HookCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.home = Path(cls.temporary.name).resolve()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_hook(self, name, payload=None, raw=None, limit=3.0):
        environment = os.environ.copy()
        environment["CLAUDE_HOOKS_HOME"] = str(self.home)
        if raw is None:
            raw = "" if payload is None else json.dumps(payload)
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, str(HOOK_DIR / name)],
            input=raw,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=5,
            check=False,
        )
        elapsed = time.perf_counter() - started
        print(f"TIMING {name} {elapsed:.3f}s", flush=True)
        # The ceiling is a regression guard sized for parallel suites, solo runs measure about 1.2 s.
        self.assertLess(elapsed, limit, f"{name} took {elapsed:.3f}s")
        return completed


class CodexFirstGuardTests(HookCase):
    def payload(self, path=None, notebook=None):
        tool_input = {}
        if path is not None:
            tool_input["file_path"] = path
        if notebook is not None:
            tool_input["notebook_path"] = notebook
        return {"tool_name": "Write", "cwd": "C:/work", "tool_input": tool_input}

    def setUp(self):
        marker = self.home / ".claude" / "direct-edit-ok"
        if marker.exists():
            marker.unlink()

    def test_python_is_blocked(self):
        result = self.run_hook("codex_first_guard.py", self.payload("C:/work/main.py"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("Codex-first", result.stderr)

    def test_markdown_is_allowed(self):
        result = self.run_hook("codex_first_guard.py", self.payload("C:/work/readme.md"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_extended_blocked_paths(self):
        for name in ("run.ps1", "config.yml", "pyproject.toml", "Dockerfile", "Dockerfile.dev"):
            with self.subTest(name=name):
                result = self.run_hook(
                    "codex_first_guard.py", self.payload(f"C:/work/{name}")
                )
                self.assertEqual(result.returncode, 2)

    def test_exempt_paths(self):
        for path in (
            "C:/work/.claude/hooks/a.py",
            "C:\\Users\\a\\AppData\\Local\\Temp\\a.py",
        ):
            with self.subTest(path=path):
                result = self.run_hook("codex_first_guard.py", self.payload(path))
                self.assertEqual(result.returncode, 0)

    def test_project_tmp_directory_is_blocked(self):
        result = self.run_hook(
            "codex_first_guard.py", self.payload("F:/proj/tmp/x.py")
        )
        self.assertEqual(result.returncode, 2)

    def test_posix_tmp_root_is_allowed(self):
        result = self.run_hook(
            "codex_first_guard.py", self.payload("/tmp/scratch/x.py")
        )
        self.assertEqual(result.returncode, 0)

    def test_notebook_path_is_blocked(self):
        result = self.run_hook(
            "codex_first_guard.py", self.payload(notebook="C:/work/book.ipynb")
        )
        self.assertEqual(result.returncode, 2)

    def test_fresh_marker_allows_and_logs(self):
        marker = self.home / ".claude" / "direct-edit-ok"
        marker.parent.mkdir(parents=True)
        marker.write_text("named test exception\n", encoding="utf-8")
        result = self.run_hook("codex_first_guard.py", self.payload("C:/work/main.py"))
        self.assertEqual(result.returncode, 0)
        log = self.home / ".claude" / "direct-edit-log.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["marker"], "named test exception")

    def test_stale_marker_blocks(self):
        marker = self.home / ".claude" / "direct-edit-ok"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("old exception", encoding="utf-8")
        old = time.time() - 31 * 60
        os.utime(marker, (old, old))
        result = self.run_hook("codex_first_guard.py", self.payload("C:/work/main.py"))
        self.assertEqual(result.returncode, 2)

    def test_invalid_inputs_allow(self):
        for raw in ("", "not json"):
            with self.subTest(raw=raw):
                result = self.run_hook("codex_first_guard.py", raw=raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


class SecretGuardTests(HookCase):
    def invoke(self, tool_name, tool_input):
        return self.run_hook(
            "secret_guard.py", {"tool_name": tool_name, "tool_input": tool_input}
        )

    def test_aws_literal_is_blocked(self):
        credential = "AK" + "IA" + "0123456789ABCDEF"
        result = self.invoke("Write", {"file_path": "safe.txt", "content": credential})
        self.assertEqual(result.returncode, 2)
        self.assertIn("AWS", result.stderr)

    def test_environment_filename_rules(self):
        blocked = self.invoke("Write", {"file_path": "C:/work/.env", "content": "benign"})
        allowed = self.invoke(
            "Write", {"file_path": "C:/work/.env.example", "content": "benign"}
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(allowed.returncode, 0)

    def test_placeholder_assignment_is_allowed(self):
        result = self.invoke(
            "Write", {"file_path": "config.txt", "content": 'password = "changeme"'}
        )
        self.assertEqual(result.returncode, 0)

    def test_literal_assignment_is_blocked(self):
        value = "Q7m" + "v9Kp" + "2L4xZ"
        result = self.invoke(
            "Write", {"file_path": "config.txt", "content": f'password = "{value}"'}
        )
        self.assertEqual(result.returncode, 2)

    def test_edit_jwt_is_blocked(self):
        token = "ey" + "Jabcdefghijk.abcdefghijk.abcdefghijk"
        result = self.invoke(
            "Edit", {"file_path": "main.txt", "new_string": token}
        )
        self.assertEqual(result.returncode, 2)

    def test_multiedit_slack_token_is_blocked(self):
        token = "xox" + "b-abcdefghijklmno"
        result = self.invoke(
            "MultiEdit",
            {
                "file_path": "main.txt",
                "edits": [{"new_string": "safe"}, {"new_string": token}],
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Slack", result.stderr)

    def test_bash_github_token_is_blocked(self):
        token = "gh" + "p_" + "abcdefghijklmnopqrstuv"
        result = self.invoke("Bash", {"command": f"export TOKEN={token}"})
        self.assertEqual(result.returncode, 2)

    def test_bash_variable_reference_is_allowed(self):
        result = self.invoke("Bash", {"command": 'export TOKEN="$TOKEN"'})
        self.assertEqual(result.returncode, 0)

    def test_powershell_dsn_is_blocked(self):
        dsn = "post" + "gresql://user:password123@db.example.invalid/db"
        result = self.invoke("PowerShell", {"command": f"$env:DATABASE_URL='{dsn}'"})
        self.assertEqual(result.returncode, 2)

    def test_hook_content_is_exempt(self):
        credential = "AK" + "IA" + "0123456789ABCDEF"
        path = self.home / ".claude" / "hooks" / "anything.py"
        result = self.invoke("Write", {"file_path": str(path), "content": credential})
        self.assertEqual(result.returncode, 0)

    def test_benign_write_is_allowed(self):
        result = self.invoke(
            "Write", {"file_path": "notes.txt", "content": "ordinary content"}
        )
        self.assertEqual(result.returncode, 0)


class SessionContextTests(HookCase):
    def create_repo(self, expected="a@b.c"):
        repo = Path(tempfile.mkdtemp(dir=self.home))
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Hook Test"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "a@b.c"], cwd=repo, check=True
        )
        (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True, exist_ok=True)
        machine = {"identities": {str(repo): expected}, "health": None}
        (local / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
        return repo

    def context(self, cwd):
        result = self.run_hook(
            "session_context.py", {"cwd": str(cwd)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        return value["hookSpecificOutput"]["additionalContext"]

    def test_matching_identity(self):
        repo = self.create_repo()
        context = self.context(repo)
        self.assertIn("branch:", context)
        self.assertIn("reflog:", context)
        self.assertIn("identity: a@b.c", context)
        self.assertIn("expected: a@b.c", context)
        self.assertNotIn("MISMATCH", context)

    def test_identity_mismatch(self):
        repo = self.create_repo("x@y.z")
        self.assertIn("MISMATCH", self.context(repo))

    def test_newest_handoff(self):
        repo = self.create_repo()
        older = repo / "HANDOFF-2026-01-01-x.md"
        newer = repo / "HANDOFF-2026-02-01-y.md"
        older.write_text("old", encoding="utf-8")
        newer.write_text("new", encoding="utf-8")
        now = time.time()
        os.utime(older, (now - 10, now - 10))
        os.utime(newer, (now, now))
        self.assertIn(newer.name, self.context(repo))

    def test_health_log(self):
        repo = self.create_repo()
        log = repo / "daily.log"
        log.write_text("ERROR first\nok\nerror second\n", encoding="utf-8")
        machine = {
            "identities": {str(repo): "a@b.c"},
            "health": {
                "type": "newest_log",
                "glob": str(repo / "*.log"),
                "error_regex": "error",
                "done_marker": "complete marker",
                "label": "Daily Test",
            },
        }
        path = self.home / ".claude" / "local" / "machine.json"
        path.write_text(json.dumps(machine), encoding="utf-8")
        context = self.context(repo)
        self.assertIn("2 error lines", context)
        self.assertIn("no completion marker", context)

    def test_non_git_directory(self):
        directory = Path(tempfile.mkdtemp(dir=self.home))
        context = self.context(directory)
        self.assertIn("branch: not a git repo", context)

    def test_compact_prepends_handoff_reminder(self):
        directory = Path(tempfile.mkdtemp(dir=self.home))
        for handoff in (None, directory / "HANDOFF-test.md"):
            if handoff is not None:
                handoff.write_text("Test state.\n", encoding="utf-8")
            for source in ("compact", "startup"):
                with self.subTest(handoff=handoff, source=source):
                    result = self.run_hook("session_context.py", {
                        "cwd": str(directory), "source": source,
                    })
                    self.assertEqual(result.returncode, 0, result.stderr)
                    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                    if source == "compact":
                        reminder = (
                            f"compacted: the context was just compacted; read {handoff} before continuing, it is the newest handoff in this folder"
                            if handoff else
                            "compacted: the context was just compacted and no handoff exists in this folder; the pre-compaction detail is gone, rely on the summary"
                        )
                        self.assertEqual(context.splitlines()[0], reminder)
                    else:
                        self.assertNotIn("compacted:", context)
                        self.assertTrue(context.startswith("cwd: "))
                    self.assertIn(f"handoff: {handoff or 'none'}", context)

    def test_compaction_clears_stamp_and_startup_keeps_it(self):
        directory = Path(tempfile.mkdtemp(dir=self.home))
        stamp = nudge_stamp(self.home, "compact-test")
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("old stamp\n", encoding="utf-8")
        for source in ("startup", "compact"):
            result = self.run_hook("session_context.py", {
                "cwd": str(directory), "source": source, "session_id": " compact-test ",
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stamp.exists(), source != "compact")

    def test_compaction_unlinks_write_error_marker(self):
        import session_context

        stamp = nudge_stamp(self.home, "compact-marker")
        marker = stamp.with_name(stamp.name + ".write-error")
        for stamp_present in (True, False):
            with self.subTest(stamp_present=stamp_present):
                if stamp_present:
                    atomic_write_text(stamp, "old stamp\n")
                atomic_write_text(marker, "28")
                with mock.patch.object(session_context, "hooks_home", return_value=self.home), mock.patch.object(session_context, "git_run", return_value="false"), mock.patch.object(session_context, "process_context", return_value="none"):
                    session_context.build_context({"cwd": str(self.home), "source": "compact", "session_id": "compact-marker"})
                self.assertFalse(stamp.exists())
                self.assertFalse(marker.exists())

    def test_startup_keeps_write_error_marker(self):
        import session_context

        stamp = nudge_stamp(self.home, "startup-marker")
        atomic_write_text(stamp, "old stamp\n")
        marker = stamp.with_name(stamp.name + ".write-error")
        atomic_write_text(marker, "28")
        with mock.patch.object(session_context, "hooks_home", return_value=self.home), mock.patch.object(session_context, "git_run", return_value="false"), mock.patch.object(session_context, "process_context", return_value="none"):
            session_context.build_context({"cwd": str(self.home), "source": "startup", "session_id": "startup-marker"})
        self.assertTrue(stamp.exists())
        self.assertTrue(marker.exists())

    def test_compaction_reports_failed_handoff_lookup(self):
        import session_context
        with mock.patch.object(Path, "glob", side_effect=OSError("lookup failed")), \
                mock.patch.object(session_context, "git_run", return_value="false"), \
                mock.patch.object(session_context, "process_context", return_value="none"), \
                mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            context = session_context.build_context({"cwd": str(self.home), "source": "compact"})
        self.assertEqual(context.splitlines()[0], "compacted: the context was just compacted; the handoff lookup failed, check the working folder for HANDOFF files before continuing")
        self.assertIn("handoff: unavailable (lookup failed)", context)

    def test_compaction_unlinks_stamp_while_locked(self):
        import session_context

        stamp = nudge_stamp(self.home, "compact-lock-test")
        atomic_write_text(stamp, "old stamp\n")
        original_unlink = Path.unlink
        attempts = []

        def unlink(path, *args, **kwargs):
            if path == stamp:
                with self.assertRaises(TimeoutError):
                    with file_lock(stamp, timeout=0):
                        self.fail("stamp removed without its lock")
                attempts.append(path)
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink), mock.patch.object(session_context, "git_run", return_value="false"), mock.patch.object(session_context, "process_context", return_value="none"), mock.patch.object(session_context, "hooks_home", return_value=self.home):
            session_context.build_context({"cwd": str(self.home), "source": "compact", "session_id": "compact-lock-test"})
        self.assertEqual(attempts, [stamp])
        self.assertFalse(stamp.exists())

    def test_compaction_without_stamp_leaves_no_files(self):
        import session_context

        home = Path(tempfile.mkdtemp(dir=self.home))
        with mock.patch.object(session_context, "hooks_home", return_value=home), mock.patch.object(session_context, "git_run", return_value="false"), mock.patch.object(session_context, "process_context", return_value="none"):
            session_context.build_context({"cwd": str(home), "source": "compact", "session_id": "probe-session"})
        self.assertEqual(list(nudge_stamp(home, "probe-session").parent.iterdir()), [])

    def test_compaction_resets_stamp_when_lock_fails(self):
        import session_context

        stamp = nudge_stamp(self.home, "compact-lock-failure")
        for error in (OSError(37, "No locks available"), TimeoutError("busy")):
            atomic_write_text(stamp, "200000 timestamp saved\n")
            with self.subTest(error=error), mock.patch.object(session_context, "file_lock", side_effect=error), mock.patch.object(session_context, "hooks_home", return_value=self.home), mock.patch.object(session_context, "git_run", return_value="false"), mock.patch.object(session_context, "process_context", return_value="none"):
                session_context.build_context({"cwd": str(self.home), "source": "compact", "session_id": "compact-lock-failure"})
            self.assertFalse(stamp.exists())


class ContextSaveNudgeTests(HookCase):
    def setUp(self):
        import context_save_nudge
        self.nudge = context_save_nudge
        self.home = Path(tempfile.mkdtemp(dir=type(self).home))
        self.stamp = nudge_stamp(self.home, "test-session")
        self.directory = self.stamp.parent
        self.transcript = self.home / "transcript.jsonl"
        self.payload = {
            "session_id": "test-session", "cwd": str(self.home),
            "transcript_path": str(self.transcript),
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
        }

    def record(self, tokens=150000, sidechain=False):
        return {"type": "assistant", "isSidechain": sidechain, "message": {
            "usage": {"input_tokens": tokens},
        }}

    def write_transcript(self, *records):
        self.transcript.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def write_machine(self, threshold):
        path = self.home / ".claude" / "local" / "machine.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"context_save_at": threshold}), encoding="utf-8")

    def write_stamp(self, age=0, tokens=150000, state="nudged"):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stamp.write_text(f"{tokens} timestamp {state}\n", encoding="utf-8")
        modified = time.time() - age
        os.utime(self.stamp, (modified, modified))

    def run_nudge(self):
        result = self.run_hook("context_save_nudge.py", self.payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def assert_silent(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_stamp_lock_timeout_is_silent(self):
        self.write_transcript(self.record())
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), mock.patch.object(self.nudge, "hooks_home", return_value=self.home), mock.patch.object(self.nudge, "file_lock", side_effect=TimeoutError("busy")) as lock, mock.patch.object(self.nudge, "read_stamp", return_value=None) as read, contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.nudge.main(), 0)
        lock.assert_called_once_with(self.stamp, timeout=2.0, discard=True)
        read.assert_called_once_with(self.stamp)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_transcript_timeout_is_diagnosed(self):
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), mock.patch.object(self.nudge, "hooks_home", return_value=self.home), mock.patch.object(self.nudge, "transcript_tokens", side_effect=TimeoutError("transcript timed out")), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.nudge.main(), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "context-save-nudge: transcript timed out\n")
        self.assertFalse(self.stamp.with_name(self.stamp.name + ".lock").exists())

    def test_existing_stamp_keeps_lock_after_early_return(self):
        self.write_transcript(self.record())
        self.assertIsNotNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        with mock.patch.object(self.nudge, "file_lock", side_effect=AssertionError("lock must not be taken on the early return")):
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertTrue(self.stamp.exists())
        self.assertTrue(self.stamp.with_name(self.stamp.name + ".lock").exists())

    def test_stamp_lock_oserror_is_diagnosed(self):
        self.write_transcript(self.record())
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), mock.patch.object(self.nudge, "hooks_home", return_value=self.home), mock.patch.object(self.nudge, "file_lock", side_effect=OSError("lock failed")), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.nudge.main(), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "context-save-nudge: lock failed\n")

    def test_stamp_reread_and_write_hold_lock(self):
        self.write_transcript(self.record())
        original_read, original_write = self.nudge.read_stamp, self.nudge.write_stamp

        def locked_call(function, *args):
            with self.assertRaises(TimeoutError):
                with file_lock(self.stamp, timeout=0):
                    self.fail("stamp is not locked")
            return function(*args)

        def read_stamp(*args):
            if read.call_count == 1:
                self.assertFalse(self.stamp.with_name(self.stamp.name + ".lock").exists())
                return original_read(*args)
            return locked_call(original_read, *args)

        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), mock.patch.object(self.nudge, "hooks_home", return_value=self.home), mock.patch.object(self.nudge, "read_stamp", side_effect=read_stamp) as read, mock.patch.object(self.nudge, "write_stamp", side_effect=lambda *args: locked_call(original_write, *args)) as write, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.nudge.main(), 0)
        self.assertEqual(read.call_count, 2)
        write.assert_called_once()
        self.assertTrue(self.stamp.with_name(self.stamp.name + ".lock").is_file())

    def test_decide_diagnoses_lock_error_only_when_errno_changes(self):
        self.write_transcript(self.record())
        errors = [OSError(37, "No locks available"), OSError(37, "No locks available"), OSError(28, "No space left")]
        with mock.patch.object(self.nudge, "file_lock", side_effect=errors), mock.patch.object(self.nudge, "write_stamp") as write, contextlib.redirect_stderr(io.StringIO()) as stderr:
            for index in range(3):
                self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
                self.assertEqual(len(stderr.getvalue().splitlines()), 1 if index < 2 else 2)
        write.assert_not_called()
        self.assertEqual(self.stamp.with_name(self.stamp.name + ".lock-error").read_text(encoding="utf-8"), "28")

    def test_lock_error_marker_write_failure_is_swallowed(self):
        self.write_transcript(self.record())
        error = OSError(28, "No space left")
        with mock.patch.object(self.nudge, "file_lock", side_effect=error), mock.patch.object(self.nudge, "atomic_write_text", side_effect=error), mock.patch.object(self.nudge, "diagnose") as diagnose:
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        diagnose.assert_called_once_with(error)

    def test_successful_lock_clears_previous_error_marker(self):
        self.write_transcript(self.record())
        with mock.patch.object(self.nudge, "file_lock", side_effect=OSError(37, "No locks available")), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        marker = self.stamp.with_name(self.stamp.name + ".lock-error")
        self.assertTrue(marker.is_file())
        self.assertIsNotNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertFalse(marker.exists())

    def test_successful_other_session_keeps_error_marker(self):
        self.write_transcript(self.record())
        with mock.patch.object(self.nudge, "file_lock", side_effect=OSError(37, "No locks available")), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        marker = self.stamp.with_name(self.stamp.name + ".lock-error")
        other = nudge_stamp(self.home, "other-session")
        self.assertIsNotNone(self.nudge.decide(other, self.payload, self.home, 150000))
        self.assertEqual(marker.read_text(encoding="utf-8"), "37")

    def test_housekeeping_preserves_marker_when_stamp_appears_before_lock(self):
        self.directory.mkdir(parents=True)
        marker = self.stamp.with_name(self.stamp.name + ".write-error")
        marker.write_text("13", encoding="utf-8")
        old = time.time() - 8 * 24 * 60 * 60
        os.utime(marker, (old, old))
        original_lock = self.nudge.file_lock

        @contextlib.contextmanager
        def lock(path, **kwargs):
            with original_lock(path, **kwargs):
                self.stamp.touch()
                yield

        with mock.patch.object(self.nudge, "file_lock", side_effect=lock):
            self.nudge.housekeeping(self.directory)
        self.assertTrue(self.stamp.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "13")

    def test_housekeeping_reaps_only_old_orphan_error_markers(self):
        self.directory.mkdir(parents=True)
        for name, days in (("old.nudged.lock-error", 8), ("fresh.nudged.lock-error", 1),
                           ("live.nudged.lock-error", 8), ("live.nudged", 1)):
            path = self.directory / name
            path.touch()
            modified = time.time() - days * 24 * 60 * 60
            os.utime(path, (modified, modified))
        self.nudge.housekeeping(self.directory)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"fresh.nudged.lock-error", "live.nudged.lock-error", "live.nudged", "live.nudged.lock"})

    def test_save_marks_racing_nudge_saved_under_lock(self):
        previous = (time.time() - 2000, 150000, "nudged")
        original_write = self.nudge.write_stamp

        def write(stamp, tokens, state):
            with self.assertRaises(TimeoutError):
                with file_lock(stamp, timeout=0):
                    self.fail("saved stamp written without lock")
            original_write(stamp, tokens, state)

        with mock.patch.object(self.nudge, "read_stamp", side_effect=[None, previous]) as read, mock.patch.object(self.nudge, "is_save", return_value=True), mock.patch.object(self.nudge, "transcript_tokens", return_value=200000) as tokens, mock.patch.object(self.nudge, "write_stamp", side_effect=write) as saved:
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertEqual(read.call_count, 2)
        tokens.assert_called_once()
        saved.assert_called_once_with(self.stamp, 200000, "saved")
        self.assertEqual(self.nudge.read_stamp(self.stamp)[2], "saved")

    def test_locked_no_action_discards_only_orphan_lock(self):
        old = (time.time() - 2000, 150000, "nudged")
        recent = (time.time(), 150000, "nudged")
        for stamp_exists in (False, True):
            if stamp_exists:
                self.write_stamp()
            with self.subTest(stamp_exists=stamp_exists), mock.patch.object(self.nudge, "read_stamp", side_effect=[old, recent]), mock.patch.object(self.nudge, "is_save", return_value=False), mock.patch.object(self.nudge, "transcript_tokens", return_value=200000), mock.patch.object(self.nudge, "write_stamp") as write, mock.patch.object(self.nudge, "housekeeping") as cleanup:
                self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
            write.assert_not_called()
            cleanup.assert_not_called()
            expected = {self.stamp.name, self.stamp.name + ".lock"} if stamp_exists else set()
            self.assertEqual({path.name for path in self.directory.iterdir()}, expected)

    def test_needs_tokens_matches_token_independent_gates(self):
        with mock.patch.object(self.nudge.time, "time", return_value=2000):
            for previous, saving, expected in ((None, True, False), (None, False, True),
                                               ((1101, 150000, "nudged"), False, False),
                                               ((1100, 150000, "nudged"), False, True),
                                               ((2000, 150000, "nudged"), True, True),
                                               ((2000, 150000, "saved"), False, True)):
                with self.subTest(previous=previous, saving=saving):
                    self.assertIs(self.nudge.needs_tokens(previous, saving), expected)
                    if not expected:
                        self.assertIsNone(self.nudge.stamp_action(previous, saving, None, 150000))

    def test_housekeeping_keeps_held_old_lock(self):
        lock = self.stamp.with_name(self.stamp.name + ".lock")
        with file_lock(self.stamp):
            modified = time.time() - 8 * 24 * 60 * 60
            os.utime(lock, (modified, modified))
            self.nudge.housekeeping(self.directory)
            self.assertTrue(lock.exists())
            self.write_stamp(age=8 * 24 * 60 * 60)
            self.nudge.housekeeping(self.directory)
            self.assertTrue(self.stamp.exists())
            self.assertTrue(lock.exists())

    def test_housekeeping_unlinks_stamp_while_locked(self):
        self.write_stamp(age=8 * 24 * 60 * 60)
        original_unlink = os.unlink
        attempts = []

        def unlink(path, *args, **kwargs):
            if Path(path) == self.stamp:
                with self.assertRaises(TimeoutError):
                    with file_lock(self.stamp, timeout=0):
                        self.fail("stamp removed without holding its lock")
                attempts.append(path)
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(os, "unlink", side_effect=unlink):
            self.nudge.housekeeping(self.directory)
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.stamp.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_housekeeping_preserves_stamp_refreshed_before_lock(self):
        self.write_stamp(age=8 * 24 * 60 * 60)

        @contextlib.contextmanager
        def refreshed_lock(path, timeout, discard):
            with file_lock(path, timeout=timeout, discard=discard):
                self.write_stamp()
                yield

        with mock.patch.object(self.nudge, "file_lock", side_effect=refreshed_lock):
            self.nudge.housekeeping(self.directory)
        self.assertTrue(self.stamp.exists())

    def test_quiet_paths_skip_lock_and_housekeeping(self):
        for previous, tokens, saving in ((None, 149999, False),
                                         ((time.time(), 150000, "nudged"), 300000, False),
                                         ((time.time(), 150000, "saved"), 200000, False)):
            with self.subTest(previous=previous, tokens=tokens, saving=saving), mock.patch.object(self.nudge, "read_stamp", return_value=previous), mock.patch.object(self.nudge, "is_save", return_value=saving), mock.patch.object(self.nudge, "transcript_tokens", return_value=tokens), mock.patch.object(self.nudge, "file_lock") as lock, mock.patch.object(self.nudge, "housekeeping") as cleanup:
                self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
            lock.assert_not_called()
            cleanup.assert_not_called()

    def test_transcript_is_read_once_before_lock(self):
        self.write_transcript(self.record())
        original_tokens = self.nudge.transcript_tokens

        def tokens(path):
            self.assertFalse(self.stamp.with_name(self.stamp.name + ".lock").exists())
            return original_tokens(path)

        with mock.patch.object(self.nudge, "transcript_tokens", side_effect=tokens) as read:
            self.assertIsNotNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        read.assert_called_once_with(str(self.transcript))

    def test_locked_reread_prevents_stale_mutations(self):
        recent = (time.time(), 150000, "nudged")
        old = (time.time() - 1000, 150000, "nudged")
        for previous, current, saving, tokens in ((None, recent, False, 150000),
                                                  (old, None, True, 150000),
                                                  (old, recent, False, 70000)):
            with self.subTest(previous=previous, saving=saving, tokens=tokens), mock.patch.object(self.nudge, "read_stamp", side_effect=[previous, current]) as read, mock.patch.object(self.nudge, "is_save", return_value=saving), mock.patch.object(self.nudge, "transcript_tokens", return_value=tokens), mock.patch.object(self.nudge, "write_stamp") as write, mock.patch.object(Path, "unlink") as unlink, mock.patch.object(self.nudge, "housekeeping") as cleanup:
                self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
            self.assertEqual(read.call_count, 2)
            write.assert_not_called()
            self.assertEqual(unlink.call_args_list, [mock.call(missing_ok=True), mock.call(missing_ok=True)])
            cleanup.assert_not_called()

    def test_compaction_reset_discards_stale_token_count(self):
        previous = (time.time() - 2000, 150000, "nudged")
        with mock.patch.object(self.nudge, "read_stamp", side_effect=[previous, None]) as read, mock.patch.object(self.nudge, "is_save", return_value=False), mock.patch.object(self.nudge, "transcript_tokens", return_value=200000), mock.patch.object(self.nudge, "write_stamp") as write:
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertEqual(read.call_count, 2)
        write.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "Windows cannot unlink an open lock file")
    def test_rearm_unlinks_while_locked(self):
        self.write_stamp(age=1000)
        self.write_transcript(self.record(70000))
        lock = self.stamp.with_name(self.stamp.name + ".lock")
        original_unlink = Path.unlink
        attempts = []

        def unlink(path, *args, **kwargs):
            if path == lock:
                with self.assertRaises(TimeoutError):
                    with file_lock(self.stamp, timeout=0):
                        self.fail("lock file removed without holding its lock")
                attempts.append(path)
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), mock.patch.object(self.nudge, "hooks_home", return_value=self.home), mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.nudge.main(), 0)
        self.assertEqual(attempts, [lock])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(lock.exists())

    def test_housekeeping_removes_only_old_stamp_locks(self):
        self.directory.mkdir(parents=True)
        for name, days in (("old.nudged.lock", 8), ("recent.nudged.lock", 1), ("live.nudged.lock", 8), ("live.nudged", 1), ("other.lock", 8)):
            path = self.directory / name
            path.touch()
            modified = time.time() - days * 24 * 60 * 60
            os.utime(path, (modified, modified))
        self.nudge.housekeeping(self.directory)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"recent.nudged.lock", "live.nudged.lock", "live.nudged", "other.lock"})

    def test_housekeeping_reaps_old_orphan_when_locking_unavailable(self):
        self.directory.mkdir(parents=True)
        for name, days in (("old.nudged.lock", 8), ("recent.nudged.lock", 1), ("live.nudged.lock", 8), ("live.nudged", 1)):
            path = self.directory / name
            path.touch()
            modified = time.time() - days * 24 * 60 * 60
            os.utime(path, (modified, modified))
        with mock.patch.object(self.nudge, "file_lock", side_effect=OSError(37, "No locks available")):
            self.nudge.housekeeping(self.directory)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"recent.nudged.lock", "live.nudged.lock", "live.nudged"})

    def test_no_payload(self):
        self.assert_silent(self.run_hook("context_save_nudge.py"))

    def test_missing_session_id(self):
        self.payload.pop("session_id")
        self.assert_silent(self.run_nudge())

    def test_invalid_session_id(self):
        for value in (None, 123, "", "  ", "../test-session", "a/b", "a\\b", "a.b", "a" * 81):
            with self.subTest(value=value):
                self.assertIsNone(self.nudge.normalize_session_id(value))
        self.payload["session_id"] = "../test-session"
        self.assert_silent(self.run_nudge())

    def test_session_id_is_stripped(self):
        self.assertEqual(self.nudge.normalize_session_id("A_0-" * 20), "A_0-" * 20)
        self.write_transcript(self.record())
        self.payload["session_id"] = "  test-session\n"
        self.assertTrue(self.run_nudge().stdout)
        self.assertTrue(self.stamp.is_file())

    def test_missing_transcript_path(self):
        self.payload.pop("transcript_path")
        self.assert_silent(self.run_nudge())

    def test_missing_transcript_file(self):
        self.assert_silent(self.run_nudge())

    def test_invalid_transcript_path(self):
        for value in (None, 5, [], str(self.home)):
            with self.subTest(value=value):
                self.assertIsNone(self.nudge.transcript_tokens(value))
        self.payload["transcript_path"] = str(self.home)
        self.assert_silent(self.run_nudge())

    def test_no_assistant_usage(self):
        self.write_transcript({"type": "user"}, self.record(sidechain=True),
                              {"type": "assistant", "message": {"usage": []}}, [], None)
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write("not json\n")
        self.assert_silent(self.run_nudge())

    def test_latest_main_thread_usage_is_used(self):
        record = self.record(10000)
        record["message"]["usage"].update({
            "cache_read_input_tokens": 140000, "cache_creation_input_tokens": 123, "output_tokens": 456,
        })
        self.write_transcript(self.record(400000), record, self.record(900000, sidechain=True))
        self.assertIn("Context is at 150579 tokens", self.run_nudge().stdout)

    def test_invalid_usage_values_are_skipped(self):
        for value in (float("nan"), float("inf"), float("-inf"), True, False, "150000", None):
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
                with self.subTest(value=value, key=key):
                    invalid = self.record()
                    invalid["message"]["usage"][key] = value
                    self.write_transcript(self.record(160000), invalid)
                    self.assertEqual(self.nudge.transcript_tokens(str(self.transcript)), 160000)
        self.assertIn("Context is at 160000 tokens", self.run_nudge().stdout)

    def test_tail_discards_partial_first_line(self):
        partial = json.dumps(self.record(900000)).encode() + b"\n"
        padding = json.dumps({"type": "user", "message": "padding"}).encode() + b"\n"
        remaining = 262144 - len(partial)
        tail = partial + padding * (remaining // len(padding)) + b" " * (remaining % len(padding))
        self.transcript.write_bytes(b'{"padding":"' + b"x" * 300000 + tail)
        self.assertIsNone(self.nudge.transcript_tokens(str(self.transcript)))
        with self.transcript.open("ab") as handle:
            handle.write(b"\n" + json.dumps(self.record(170000)).encode() + b"\n")
        self.assertIn("Context is at 170000 tokens", self.run_nudge().stdout)

    def test_tail_grows_past_large_non_assistant_record(self):
        self.write_transcript(self.record(170000), {"type": "user", "message": "x" * 409600})
        self.assertIn("Context is at 170000 tokens", self.run_nudge().stdout)

    def test_large_transcript_without_assistant_is_silent(self):
        self.write_transcript({"type": "user", "message": "x" * 800000})
        self.assert_silent(self.run_nudge())

    def test_window_cap_streams_older_records(self):
        padding = {"type": "user", "message": "x" * 800000}
        with mock.patch.object(self.nudge, "MAX_TRANSCRIPT_WINDOW", 524288):
            self.write_transcript(self.record(180000), padding)
            self.assertEqual(self.nudge.transcript_tokens(str(self.transcript)), 180000)
            self.write_transcript(padding)
            self.assertIsNone(self.nudge.transcript_tokens(str(self.transcript)))

    def test_subagents_exit_before_file_reads(self):
        output, errors = io.StringIO(), io.StringIO()
        for key in ("agent_id",):
            with self.subTest(key=key), \
                    mock.patch.object(self.nudge, "read_payload", return_value={**self.payload, key: "test-agent"}), \
                    mock.patch.object(self.nudge, "transcript_tokens") as transcript, \
                    mock.patch.object(self.nudge, "load_machine") as machine, \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(self.nudge.main(), 0)
                transcript.assert_not_called()
                machine.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.write_transcript(self.record())
        self.payload["agent_id"] = "test-agent"
        self.assert_silent(self.run_nudge())

    def test_disabled_threshold(self):
        self.write_transcript(self.record())
        self.write_machine(0)
        self.assert_silent(self.run_nudge())
        self.assertEqual({path.name for path in self.directory.iterdir()}, {".swept"})

    def test_agent_type_without_agent_id_still_nudges(self):
        self.write_transcript(self.record())
        self.payload["agent_type"] = "test-agent"
        self.assertTrue(self.run_nudge().stdout)

    def test_zero_usage_skips_record_and_preserves_stamp(self):
        self.write_stamp(age=1000)
        self.write_transcript(self.record(170000), self.record(0))
        self.assertIsNone(self.nudge.usage_tokens(json.dumps(self.record(0))))
        self.assertIn("Context is at 170000 tokens", self.run_nudge().stdout)
        self.assertTrue(self.stamp.exists())

    def test_unicode_line_separator_does_not_split_record(self):
        record = self.record(175000)
        record["message"]["content"] = "first\u2028second\u2029third\u0085fourth"
        self.transcript.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertIn("Context is at 175000 tokens", self.run_nudge().stdout)
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "message": "x" * 800000}) + "\n")
        with mock.patch.object(self.nudge, "MAX_TRANSCRIPT_WINDOW", 524288):
            self.assertEqual(self.nudge.transcript_tokens(str(self.transcript)), 175000)

    def test_disable_value_table(self):
        for value, disabled in ((0, True), (0.0, True), (False, True), (-1, False),
                                ("0", False), (None, False), (True, False), (150000, False)):
            with self.subTest(value=value):
                self.assertIs(nudge_disabled(value), disabled)
                self.assertEqual(self.nudge.save_threshold(value), 0 if disabled else 150000)
        self.write_machine(False)
        self.write_transcript(self.record())
        self.assert_silent(self.run_nudge())

    def test_save_without_stamp_reads_transcript_and_stays_silent(self):
        self.payload.update({"tool_name": "Write", "tool_input": {"file_path": "F:/x/HANDOFF-2026-09-14-a.md"}})
        self.assert_silent(self.run_nudge())
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), \
                mock.patch.object(self.nudge, "transcript_tokens", return_value=200000) as transcript, \
                mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            self.assertEqual(self.nudge.main(), 0)
            transcript.assert_called_once_with(str(self.transcript))
        self.assertFalse(self.stamp.exists())
        self.assertEqual({path.name for path in self.directory.iterdir()}, {".swept"})

    def test_unrelated_write_is_not_a_save(self):
        self.write_transcript(self.record())
        self.payload.update({"tool_name": "Write", "tool_input": {"file_path": "F:/x/notes.md"}})
        self.assertTrue(self.run_nudge().stdout)

    def test_bash_call_is_never_a_save(self):
        self.write_transcript(self.record())
        self.payload["tool_input"] = {"file_path": "F:/x/HANDOFF-test.md"}
        self.assertTrue(self.run_nudge().stdout)

    def test_plan_path_and_payload_validation(self):
        for path, expected in ((str(self.home / ".claude/plans/a.md"), True),
                               ("~/.claude/plans/subdir/a.md", True),
                               ("~/.claude/plans-other/a.md", False),
                               ("~/.claude/plans/../a.md", False), (None, False), (5, False)):
            with self.subTest(path=path):
                payload = {"tool_name": "Edit", "tool_input": {"file_path": path}}
                self.assertIs(self.nudge.is_save(payload, self.home), expected)
        self.assertFalse(self.nudge.is_save({"tool_name": "Write", "tool_input": None}, self.home))

    def test_stamp_failure_never_nudges_and_marker_gates_diagnostic(self):
        self.write_transcript(self.record())
        original_write = self.nudge.atomic_write_text
        marker = self.stamp.with_name(self.stamp.name + ".write-error")
        for all_paths in (True, False):
            def write(path, content):
                if all_paths or path == self.stamp:
                    raise OSError(13, "denied")
                return original_write(path, content)

            with self.subTest(all_paths=all_paths), mock.patch.object(self.nudge, "atomic_write_text", side_effect=write), contextlib.redirect_stderr(io.StringIO()) as stderr:
                contexts = [self.nudge.decide(self.stamp, self.payload, self.home, 150000) for _ in range(5)]
            self.assertEqual([value is not None for value in contexts], [False] * 5)
            self.assertEqual(len(stderr.getvalue().splitlines()), 5 if all_paths else 1)
            self.assertEqual(marker.exists(), not all_paths)

    def test_stamp_removal_rearms_write_error_notification(self):
        self.nudge.write_stamp(self.stamp, 150000, "nudged")
        old = time.time() - 2000
        os.utime(self.stamp, (old, old))
        original_write = self.nudge.atomic_write_text
        marker = self.stamp.with_name(self.stamp.name + ".write-error")

        def write(path, content):
            if path == self.stamp:
                raise OSError(13, "denied")
            return original_write(path, content)

        with mock.patch.object(self.nudge, "atomic_write_text", side_effect=write), mock.patch.object(self.nudge, "transcript_tokens", side_effect=[200000, 20000, 200000]), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
            self.assertEqual(marker.read_text(encoding="utf-8"), "13")
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
            self.assertFalse(self.stamp.exists())
            self.assertFalse(marker.exists())
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertEqual(len(stderr.getvalue().splitlines()), 2)

    def test_stamp_write_failure_is_silent_and_diagnosed_once(self):
        self.write_transcript(self.record())
        original_write = self.nudge.atomic_write_text

        def write(path, content):
            if path == self.stamp:
                raise OSError(28, "write failed")
            return original_write(path, content)

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), \
                mock.patch.object(self.nudge, "atomic_write_text", side_effect=write), \
                mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(self.nudge.main(), 0)
            self.assertEqual(output.getvalue(), "")
            first = output.getvalue(), errors.getvalue()
            self.assertEqual(self.nudge.main(), 0)
        self.assertEqual((output.getvalue(), errors.getvalue()), first)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("write failed", errors.getvalue())

    def test_decide_never_nudges_on_stamp_write_failure(self):
        self.write_transcript(self.record())
        original_write = self.nudge.atomic_write_text

        def write(path, content):
            if path == self.stamp:
                raise OSError(28, "write failed")
            return original_write(path, content)

        with mock.patch.object(self.nudge, "atomic_write_text", side_effect=write), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)

    def test_successful_stamp_write_clears_error_marker(self):
        self.write_transcript(self.record())
        original_write = self.nudge.atomic_write_text

        def write(path, content):
            if path == self.stamp:
                raise OSError(28, "write failed")
            return original_write(path, content)

        marker = self.stamp.with_name(self.stamp.name + ".write-error")
        with mock.patch.object(self.nudge, "atomic_write_text", side_effect=write), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertEqual(marker.read_text(encoding="utf-8"), "28")
        self.assertIsNotNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertIsNone(self.nudge.decide(self.stamp, self.payload, self.home, 150000))
        self.assertTrue(self.stamp.exists())
        self.assertFalse(marker.exists())

    def test_housekeeping_reaps_only_old_orphan_write_errors(self):
        self.directory.mkdir(parents=True)
        for name, days in (("old.nudged.write-error", 8), ("fresh.nudged.write-error", 1),
                           ("live.nudged.write-error", 8), ("live.nudged", 1)):
            path = self.directory / name
            path.touch()
            modified = time.time() - days * 24 * 60 * 60
            os.utime(path, (modified, modified))
        self.nudge.housekeeping(self.directory)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"fresh.nudged.write-error", "live.nudged.write-error", "live.nudged", "live.nudged.lock"})

    def test_write_stamp_reports_success(self):
        self.assertTrue(self.nudge.write_stamp(self.stamp, 150000, "nudged"))
        self.assertEqual(self.nudge.read_stamp(self.stamp)[1:], (150000, "nudged"))

    def test_saved_state_rearms_below_half(self):
        self.write_stamp(state="saved")
        self.write_transcript(self.record(70000))
        self.assert_silent(self.run_nudge())
        self.assertFalse(self.stamp.exists())

    def test_save_precedes_rearm(self):
        self.write_stamp(age=1000)
        self.write_transcript(self.record(70000))
        self.payload.update({"tool_name": "Write", "tool_input": {"file_path": "handoff.md"}})
        self.assert_silent(self.run_nudge())
        self.assertRegex(self.stamp.read_text(encoding="utf-8"), r"\A70000 \S+ saved\n\Z")

    def test_legacy_stamp_defaults_to_nudged(self):
        self.write_stamp()
        self.stamp.write_text("150000 timestamp\n", encoding="utf-8")
        self.assertEqual(self.nudge.read_stamp(self.stamp)[1:], (150000, "nudged"))

    def test_float_threshold(self):
        self.assertEqual(self.nudge.save_threshold(150000.5), 150000.5)
        self.write_machine(150000.5)
        self.write_transcript(self.record(150001))
        self.assertIn("Context is at 150001 tokens, past the 150000 token save threshold.", self.run_nudge().stdout)

    def test_invalid_threshold_uses_default(self):
        for value in (None, -1, "100000", True, [], {}, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(self.nudge.save_threshold(value), 150000)
        for value in (0, 0.0, 150000, 150000.5):
            with self.subTest(value=value):
                self.assertEqual(self.nudge.save_threshold(value), value)
        self.write_machine(True)
        self.write_transcript(self.record())
        self.assertIn("past the 150000 token save threshold.", self.run_nudge().stdout)


    def test_invalid_session_exits_before_loading_machine(self):
        with mock.patch.object(self.nudge, "read_payload", return_value={"session_id": "../invalid"}), \
                mock.patch.object(self.nudge, "load_machine") as load:
            self.assertEqual(self.nudge.main(), 0)
            load.assert_not_called()

    def test_rearm_below_half(self):
        self.write_stamp(age=1000)
        self.write_transcript(self.record(74999))
        self.assert_silent(self.run_nudge())
        self.assertFalse(self.stamp.exists())


    def test_handoff_write_marks_stamp_saved(self):
        self.write_stamp()
        self.write_transcript(self.record(170000))
        self.payload.update({"tool_name": "Write", "tool_input": {"file_path": "F:/x/HANDOFF-2026-09-14-a.md"}})
        self.assert_silent(self.run_nudge())
        self.assertRegex(self.stamp.read_text(encoding="utf-8"), r"\A170000 \S+ saved\n\Z")


    def test_saved_session_below_growth_threshold_is_silent(self):
        self.write_stamp(age=1000, state="saved")
        self.write_transcript(self.record(299999))
        self.assert_silent(self.run_nudge())


    def test_saved_session_above_growth_threshold_nudges(self):
        self.write_stamp(age=60, state="saved")
        self.write_transcript(self.record(300001))
        self.assertIn("Context is at 300001 tokens", self.run_nudge().stdout)
        self.assertRegex(self.stamp.read_text(encoding="utf-8"), r"\A300001 \S+ nudged\n\Z")


    def test_saved_session_at_growth_threshold_nudges(self):
        self.write_stamp(age=60, state="saved")
        self.write_transcript(self.record(300000))
        self.assertTrue(self.run_nudge().stdout)


    def test_plan_edit_marks_stamp_saved(self):
        self.write_stamp()
        self.write_transcript(self.record(180000))
        self.payload.update({"tool_name": "Edit", "tool_input": {"file_path": "~/.claude/plans/plan.md"}})
        self.assert_silent(self.run_nudge())
        self.assertRegex(self.stamp.read_text(encoding="utf-8"), r"\A180000 \S+ saved\n\Z")


    def test_multiedit_handoff_is_a_save(self):
        self.write_stamp(age=1000)
        self.write_transcript(self.record(180000))
        self.payload.update({"tool_name": "MultiEdit", "tool_input": {"file_path": "F:\\x\\project_handoff.md"}})
        self.assert_silent(self.run_nudge())
        self.assertTrue(self.stamp.read_text(encoding="utf-8").endswith(" saved\n"))


    def test_unparsable_stamp_uses_zero_tokens_and_nudged_state(self):
        self.write_stamp()
        self.stamp.write_text("invalid stamp\n", encoding="utf-8")
        _, tokens, state = self.nudge.read_stamp(self.stamp)
        self.assertEqual((tokens, state), (0, "nudged"))


    def test_stamp_read_errors_count_as_absent(self):
        for error in (FileNotFoundError(), PermissionError(), OSError("read failed")):
            for operation in ("stat", "read_text"):
                with self.subTest(error=error, operation=operation):
                    stamp = mock.Mock()
                    stamp.stat.return_value.st_mtime = time.time()
                    getattr(stamp, operation).side_effect = error
                    self.assertIsNone(self.nudge.read_stamp(stamp))

    def test_rearm_ignores_stamp_removed_concurrently(self):
        self.write_transcript(self.record(70000))
        stamp = mock.Mock()
        stamp.name = self.stamp.name
        stamp.parent = self.directory
        stamp.stat.return_value.st_mtime = time.time() - 1000
        stamp.read_text.return_value = "150000 timestamp nudged"
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), \
                mock.patch.object(self.nudge, "nudge_stamp", return_value=stamp), \
                mock.patch.object(self.nudge, "file_lock", return_value=contextlib.nullcontext()), \
                mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            self.assertEqual(self.nudge.main(), 0)
        stamp.unlink.assert_called_once_with(missing_ok=True)

    def test_plan_save_does_not_suppress_renudge(self):
        self.write_stamp(age=1000)
        self.write_transcript(self.record())
        plans = self.home / ".claude" / "plans"
        plans.mkdir()
        (plans / "plan.md").write_text("Saved plan.\n", encoding="utf-8")
        self.assertTrue(self.run_nudge().stdout)

    def test_old_stamp_below_threshold_is_silent(self):
        self.write_stamp(age=901)
        self.write_transcript(self.record(90000))
        old = self.home / "HANDOFF-old.md"
        old.write_text("Older save.\n", encoding="utf-8")
        modified = time.time() - 2000
        os.utime(old, (modified, modified))
        self.assert_silent(self.run_nudge())
        self.assertEqual(self.stamp.read_text(encoding="utf-8"), "150000 timestamp nudged\n")

    def test_old_stamp_at_threshold_renudges(self):
        self.write_stamp(age=901)
        self.write_transcript(self.record())
        self.assertTrue(self.run_nudge().stdout)
        self.assertGreater(self.stamp.stat().st_mtime, time.time() - 10)


    def test_recent_stamp_skips_transcript_read(self):
        self.write_stamp()
        self.assertFalse(self.transcript.exists())
        self.assert_silent(self.run_nudge())
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), \
                mock.patch.object(self.nudge, "transcript_tokens") as transcript, \
                mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            self.assertEqual(self.nudge.main(), 0)
            transcript.assert_not_called()

    def test_at_threshold_fires_exact_text_and_stamp(self):
        self.write_transcript(self.record())
        result = self.run_nudge()
        self.assertEqual(json.loads(result.stdout), {"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "Context is at 150000 tokens, past the 150000 token save threshold. Run the save skill with the handoff trigger now: write HANDOFF-YYYY-MM-DD-<topic>.md in the working folder with the Write tool (the hook recognises the save by that write), then continue the task from where it was without asking. The user did not type this; it comes from the context_save_nudge hook.",
        }})
        self.assertRegex(self.stamp.read_text(encoding="utf-8"),
                         r"\A150000 \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00 nudged\n\Z")
        self.assert_silent(self.run_nudge())

    def test_below_threshold(self):
        self.write_transcript(self.record(149999))
        self.assert_silent(self.run_nudge())
        self.assertFalse(self.stamp.exists())
        self.assertEqual({path.name for path in self.directory.iterdir()}, {".swept"})

    def test_due_sweep_removes_old_stamps_below_threshold(self):
        self.directory.mkdir(parents=True)
        now = time.time()
        for name, days in (("old.nudged", 8), ("old.nudged.lock", 8), ("old.json", 8), ("recent.nudged", 1), ("keep.txt", 8)):
            path = self.directory / name
            path.write_text("test\n", encoding="utf-8")
            modified = now - days * 24 * 60 * 60
            os.utime(path, (modified, modified))
        nested = self.directory / "directory.nudged"
        nested.mkdir()
        os.utime(nested, (now - 8 * 24 * 60 * 60,) * 2)
        self.write_transcript(self.record(149999))
        self.assert_silent(self.run_nudge())
        self.assertFalse((self.directory / "old.nudged").exists())
        self.assertFalse((self.directory / "old.nudged.lock").exists())
        self.assertFalse(self.stamp.with_name(self.stamp.name + ".lock").exists())
        self.write_transcript(self.record())
        self.assertTrue(self.run_nudge().stdout)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {
            "test-session.nudged", "test-session.nudged.lock", "old.json", "recent.nudged", "keep.txt", "directory.nudged", ".swept",
        })

    def test_fresh_sweep_marker_skips_housekeeping(self):
        self.write_stamp(age=8 * 24 * 60 * 60)
        marker = self.directory / ".swept"
        marker.touch()
        recent = time.time() - 1
        os.utime(marker, (recent, recent))
        with mock.patch.object(self.nudge, "housekeeping") as cleanup:
            self.nudge.sweep_if_due(self.directory)
        cleanup.assert_not_called()
        self.assertTrue(self.stamp.exists())

    def test_old_sweep_marker_runs_housekeeping(self):
        self.write_stamp(age=8 * 24 * 60 * 60)
        marker = self.directory / ".swept"
        marker.touch()
        old = time.time() - 25 * 60 * 60
        os.utime(marker, (old, old))
        self.nudge.sweep_if_due(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [marker])
        self.assertGreater(marker.stat().st_mtime, old)

    def test_future_sweep_marker_runs_housekeeping(self):
        self.write_stamp(age=8 * 24 * 60 * 60)
        marker = self.directory / ".swept"
        marker.touch()
        future = time.time() + 24 * 60 * 60
        os.utime(marker, (future, future))
        self.write_transcript(self.record(149999))
        self.assert_silent(self.run_nudge())
        self.assertFalse(self.stamp.exists())
        self.assertLess(abs(marker.stat().st_mtime - time.time()), 60)

    def test_disabled_threshold_still_sweeps(self):
        self.write_machine(0)
        self.write_stamp(age=8 * 24 * 60 * 60, state="saved")
        self.assert_silent(self.run_nudge())
        self.assertEqual({path.name for path in self.directory.iterdir()}, {".swept"})

    def test_nested_plan_is_not_a_save(self):
        self.write_stamp(age=1000)
        self.write_transcript(self.record())
        nested = self.home / ".claude" / "plans" / "nested"
        nested.mkdir(parents=True)
        (nested / "plan.md").write_text("Nested plan.\n", encoding="utf-8")
        self.assertTrue(self.run_nudge().stdout)

    def test_unexpected_exception_has_one_line_diagnostic(self):
        self.write_transcript(self.record())
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(self.nudge, "read_payload", return_value=self.payload), \
                mock.patch.object(self.nudge, "load_machine", side_effect=OSError("read failed\nsecond line")), \
                mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(self.nudge.main(), 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "context-save-nudge: read failed second line\n")


class PermissionDeniedLogTests(HookCase):
    def test_denial_is_logged_and_truncated(self):
        payload = {
            "tool_name": "Bash",
            "reason": "Blocked by classifier",
            "cwd": "C:/work",
            "tool_input": {"command": "x" * 700},
        }
        result = self.run_hook("permission_denied_log.py", payload)
        self.assertEqual(result.returncode, 0)
        path = self.home / ".claude" / "denials.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        value = json.loads(lines[0])
        self.assertEqual(len(value["tool_input"]["command"]), 500)
        self.assertIs(value["tool_input"]["truncated"], True)


class DraftWrapGuardTests(HookCase):
    wrapped = (
        "This paragraph has been split across several lines\n"
        "and its next line continues the same thought\n"
        "with a final line ending the paragraph."
    )

    def setUp(self):
        marker = self.home / ".claude" / "wrap-ok"
        if marker.is_file():
            marker.unlink()

    def run_draft(self, path="draft.md", tool="Write", **tool_input):
        tool_input["file_path"] = path
        return self.run_hook("draft_wrap_guard.py", {
            "tool_name": tool, "tool_input": tool_input,
        })

    def draft_file(self, content):
        path = self.home / self._testMethodName / "draft.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def test_edit_absent_old_string_is_allowed(self):
        path = self.draft_file("A clean paragraph.")
        result = self.run_draft(path=str(path), tool="Edit", old_string="absent", new_string=self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replace_all_reports_both_inserted_paragraphs(self):
        path = self.draft_file("TOKEN\n\nTOKEN")
        result = self.run_draft(path=str(path), tool="Edit", old_string="TOKEN",
                                new_string=self.wrapped, replace_all=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 1 (", result.stderr)
        self.assertIn("and 1 more paragraphs", result.stderr)

    def test_edit_only_checks_first_occurrence_by_default(self):
        path = self.draft_file("```text\nTOKEN\n```\n\nTOKEN")
        result = self.run_draft(path=str(path), tool="Edit", old_string="TOKEN", new_string=self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_edit_ignores_existing_wrapped_paragraph(self):
        path = self.draft_file(self.wrapped + "\n\nTOKEN")
        result = self.run_draft(path=str(path), tool="Edit", old_string="TOKEN", new_string="Clean.")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_edit_empty_insertion_and_crlf_line_numbers(self):
        path = self.draft_file("First.\r\n\r\nTOKEN")
        result = self.run_draft(path=str(path), tool="Edit", old_string="TOKEN", new_string=self.wrapped)
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 3 (", result.stderr)
        result = self.run_draft(path=str(path), tool="Edit", old_string="", new_string=self.wrapped + "\n\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 1 (", result.stderr)

    def test_edit_append_matches_lf_payload_on_both_line_endings(self):
        original = "First paragraph.\nSecond sentence.\n"
        path = self.draft_file("")
        for ending in ("\r\n", "\n"):
            with self.subTest(ending=ending):
                content = original.replace("\n", ending).encode("utf-8")
                path.write_bytes(content)
                result = self.run_draft(path=str(path), tool="Edit", old_string=original,
                                        new_string=original + "\n" + self.wrapped)
                self.assertEqual(result.returncode, 2)
                self.assertIn("hard-wrapped at line 4 (", result.stderr)
                self.assertEqual(path.read_bytes(), content)

    def test_multiedit_normalizes_old_and_new_strings(self):
        original = "First paragraph.\nSecond sentence.\n"
        path = self.draft_file(original)
        interim = original + "\nTOKEN"
        result = self.run_draft(path=str(path), tool="MultiEdit", edits=[
            {"old_string": original.replace("\n", "\r\n"),
             "new_string": interim.replace("\n", "\r\n")},
            {"old_string": interim, "new_string": original + "\n" + self.wrapped},
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("edit 2, line 4 (", result.stderr)

    def test_absent_old_string_after_normalization_is_allowed(self):
        path = self.draft_file("")
        path.write_bytes(b"First paragraph.\r\nSecond sentence.\r\n")
        result = self.run_draft(path=str(path), tool="Edit",
                                old_string="Absent paragraph.\n", new_string=self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiedit_updates_prior_spans_when_lines_shift(self):
        path = self.draft_file("TITLE\n\nTOKEN")
        result = self.run_draft(path=str(path), tool="MultiEdit", edits=[
            {"old_string": "TOKEN", "new_string": self.wrapped},
            {"old_string": "TITLE", "new_string": "Title.\n\nAnother paragraph."},
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("edit 1, line 5 (", result.stderr)

    def test_multiedit_can_remove_a_previous_finding(self):
        path = self.draft_file("TOKEN")
        result = self.run_draft(path=str(path), tool="MultiEdit", edits=[
            {"old_string": "TOKEN", "new_string": self.wrapped},
            {"old_string": self.wrapped, "new_string": "Clean."},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiedit_missing_file_uses_first_edit_as_document(self):
        result = self.run_draft(tool="MultiEdit", edits=[
            {"old_string": "", "new_string": "TOKEN"},
            {"old_string": "TOKEN", "new_string": self.wrapped},
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("edit 2, line 1 (", result.stderr)

    def test_unreadable_and_oversized_targets_are_allowed(self):
        path = self.draft_file("A" * (2 * 1024 * 1024))
        directory = path.parent / "unreadable.md"
        directory.mkdir()
        for target in (path, directory):
            with self.subTest(target=target):
                result = self.run_draft(path=str(target),
                                        tool="Edit", old_string="A", new_string=self.wrapped)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapped_write_is_blocked(self):
        result = self.run_draft(content=self.wrapped)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, (
            "draft_wrap_guard: draft.md looks hard-wrapped at line 1 "
            f"({self.wrapped.splitlines()[0]}). Write each paragraph as one line; "
            "only commit bodies wrap at 72 columns."
            " To write wrapped text on purpose, put one line saying why into "
            "~/.claude/wrap-ok (valid 30 minutes).\n"
        ))

    def test_single_line_paragraphs_are_allowed(self):
        result = self.run_draft(content="One paragraph.\n\nAnother paragraph.")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_write_is_allowed(self):
        result = self.run_draft(path="draft.py", content="# " + self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapped_edit_is_blocked(self):
        path = self.draft_file("First paragraph.")
        result = self.run_draft(path=str(path), tool="Edit", old_string="First paragraph.",
                                new_string="First paragraph.\n\n" + self.wrapped)
        self.assertEqual(result.returncode, 2)
        self.assertIn("hard-wrapped at line 3 (", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), "First paragraph.")

    def test_wrapped_multiedit_is_blocked(self):
        path = self.draft_file("First.\n\nTOKEN")
        result = self.run_draft(path=str(path), tool="MultiEdit", edits=[
            {"old_string": "First.", "new_string": "A clean paragraph."},
            {"old_string": "TOKEN", "new_string": self.wrapped},
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("hard-wrapped at edit 2, line 3 (", result.stderr)

    def test_multiedit_keeps_edits_separate(self):
        path = self.draft_file("FIRST\n\nSECOND")
        result = self.run_draft(path=str(path), tool="MultiEdit", edits=[
            {"old_string": "FIRST", "new_string": self.wrapped.splitlines()[0]},
            {"old_string": "SECOND", "new_string": "and a separate edit"},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_commit_message_is_allowed(self):
        result = self.run_draft(path="COMMIT_EDITMSG", content=self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exempt_paths_are_allowed(self):
        for path in (
            "repo/.git/draft.md", "COMMIT_EDITMSG.md", "MERGE_MSG.txt",
            "CHANGELOG.md", "node_modules/draft.md", ".venv/draft.txt",
            "C:\\repo\\.git\\draft.md", "repo/node_modules/draft.md",
        ):
            with self.subTest(path=path):
                result = self.run_draft(path=path, content=self.wrapped)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_uppercase_suffix_is_blocked(self):
        result = self.run_draft(path="draft.TXT", content=self.wrapped)
        self.assertEqual(result.returncode, 2)

    def test_more_paragraphs_are_counted(self):
        result = self.run_draft(content=self.wrapped + "\n\n" + self.wrapped)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.splitlines()[1], "draft_wrap_guard: and 1 more paragraphs")

    def test_malformed_payload_is_allowed(self):
        for raw in ("", "not json", "[]", '{"tool_name": [], "tool_input": {}}'):
            with self.subTest(raw=raw):
                result = self.run_hook("draft_wrap_guard.py", raw=raw)
                self.assertEqual(result.returncode, 0, result.stderr)
        for tool_input in (None, {}, {"file_path": []}, {"file_path": "draft.md", "content": []}):
            with self.subTest(tool_input=tool_input):
                result = self.run_hook("draft_wrap_guard.py", {
                    "tool_name": "Write", "tool_input": tool_input,
                })
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_multiedit_is_allowed(self):
        for edits in (None, {}, [None], [{"new_string": self.wrapped}, {"new_string": []}]):
            with self.subTest(edits=edits):
                result = self.run_draft(tool="MultiEdit", edits=edits)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unrelated_tool_is_allowed(self):
        result = self.run_draft(tool="NotebookEdit", content=self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_file_edit_behaves_like_write(self):
        result = self.run_draft(tool="Edit", new_string=self.wrapped)
        self.assertEqual(result.returncode, 2)
        self.assertIn("hard-wrapped at line 1 (", result.stderr)

    def test_edit_inside_fence_is_allowed(self):
        old = "python tools/runner.py execute requested task\nand read the generated task output"
        new = old + "\nwith additional command arguments here"
        path = self.draft_file("```sh\n" + old + "\n```\n")
        result = self.run_draft(path=str(path), tool="Edit", old_string=old, new_string=new)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_edit_preserves_known_fence_context(self):
        result = self.run_draft(tool="Edit", new_string="```text\n\n" + self.wrapped + "\n```")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_edit_indented_continuation_is_allowed(self):
        path = self.draft_file("- List item\n  TOKEN")
        new = "\n".join("  " + line for line in self.wrapped.splitlines())
        result = self.run_draft(path=str(path), tool="Edit", old_string="  TOKEN", new_string=new)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiedit_preserves_fence_context(self):
        path = self.draft_file("```text\nFIRST\n\nSECOND\n```")
        result = self.run_draft(path=str(path), tool="MultiEdit", edits=[
            {"old_string": "FIRST", "new_string": self.wrapped},
            {"old_string": "SECOND", "new_string": self.wrapped},
        ])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiedit_counts_across_edits(self):
        path = self.draft_file("FIRST\n\nSECOND")
        result = self.run_draft(path=str(path), tool="MultiEdit", edits=[
            {"old_string": "FIRST", "new_string": self.wrapped},
            {"old_string": "SECOND", "new_string": self.wrapped},
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("edit 1, line 1 (", result.stderr)
        self.assertIn("and 1 more paragraphs", result.stderr)

    def test_fresh_marker_allows_wrapped_text(self):
        marker = self.home / ".claude" / "wrap-ok"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("Preserve intentional line breaks.\n", encoding="utf-8")
        result = self.run_draft(content=self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stale_marker_blocks_wrapped_text(self):
        marker = self.home / ".claude" / "wrap-ok"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("Preserve intentional line breaks.\n", encoding="utf-8")
        old = time.time() - 31 * 60
        os.utime(marker, (old, old))
        result = self.run_draft(content=self.wrapped)
        self.assertEqual(result.returncode, 2)

    def test_scratch_commit_files_are_allowed(self):
        for path in ("commit-msg.txt", "COMMIT-body.md", "merge_msg.txt", "merge-msg.md"):
            with self.subTest(path=path):
                result = self.run_draft(path=path, content=self.wrapped)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_commitment_and_embedded_commit_are_checked(self):
        for path in ("commitment-post.md", "draft-commit-body.md", "CUSTOM_MSG.txt"):
            with self.subTest(path=path):
                result = self.run_draft(path=path, content=self.wrapped)
                self.assertEqual(result.returncode, 2)

    def test_capitalized_new_thought_is_allowed(self):
        result = self.run_draft(content="Next steps for the collector rollout\nA second paragraph starts a new thought.\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_line_command_edit_is_allowed_inside_fence(self):
        command = "python tools/pr_review_runner.py --base main --json\nand read pr-review-findings.json"
        path = self.draft_file("```sh\nTOKEN\n```")
        result = self.run_draft(path=str(path), tool="Edit", old_string="TOKEN", new_string=command)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fresh_marker_appends_audit_record(self):
        marker = self.home / ".claude" / "wrap-ok"
        marker.parent.mkdir(parents=True, exist_ok=True)
        reason = "Preserve intentional wrapping.\n" + "Context " * 40
        marker.write_text(reason, encoding="utf-8")
        log = self.home / ".claude" / "wrap-ok-log.jsonl"
        before = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        result = self.run_draft(content=self.wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(before) + 1)
        record = json.loads(lines[-1])
        self.assertEqual(set(record), {"ts", "path", "marker", "cwd", "guard", "findings"})
        self.assertTrue(record["ts"])
        self.assertEqual(record["path"], "draft.md")
        self.assertEqual(record["marker"], reason.strip()[:200])
        self.assertIsNone(record["cwd"])
        self.assertEqual(record["guard"], "draft_wrap_guard")
        self.assertEqual(record["findings"], 1)
        result = self.run_draft(content="A clean paragraph.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), lines)
        self.assertFalse((self.home / ".claude" / "direct-edit-log.jsonl").exists())

    def test_write_headings_separate_paragraphs(self):
        result = self.run_draft(content="\n".join("## Heading\n" + self.wrapped for _ in range(3)))
        self.assertEqual(result.returncode, 2)
        self.assertIn("hard-wrapped at line 2 (", result.stderr)
        self.assertIn("and 2 more paragraphs", result.stderr)


class InstallerTests(unittest.TestCase):
    @contextlib.contextmanager
    def gh_credentials_fixture(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home with spaces"
            home.mkdir()
            yield module, module.Installer(home, REPO, False), home

    def gh_config(self, home, key, cwd=None):
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(home / ".gitconfig"), HOME=str(home))
        return subprocess.run(["git", "config", "--get-all", key], cwd=cwd or home, env=environment, capture_output=True, text=True, timeout=10)

    def test_gh_credentials_block_and_fragments(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            machine = {"gh_accounts": {"~/projects/nested": "nested", "default": "personal", "~/projects": "work"}}
            module.install_gh_credentials(installer, machine)
            config = (home / ".gitconfig").read_text()
            self.assertIn('# harness gh accounts begin\n', config)
            self.assertIn('# harness gh accounts end\n', config)
            self.assertLess(config.index("gitconfig-gh-projects\""), config.index("gitconfig-gh-projects-nested\""))
            for host in ("github.com", "gist.github.com"):
                helpers = self.gh_config(home, f"credential.https://{host}.helper")
                self.assertEqual(helpers.returncode, 0, helpers.stderr)
                interpreter = str(Path(sys.executable).resolve()).replace("\\", "/")
                self.assertEqual(helpers.stdout.splitlines(), ["", f'!"{interpreter}" -I "{home.as_posix()}/.claude/hooks/gh_credential_helper.py"', '!gh auth git-credential'])
                for folder, expected in (("elsewhere", "personal"), ("projects/repo", "work"), ("projects/nested/repo", "nested")):
                    repo = home / folder
                    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
                    result = self.gh_config(home, f"credential.https://{host}.username", repo)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines()[-1], expected)
            for slug, login in (("projects", "work"), ("projects-nested", "nested")):
                fragment = home / ".claude/local" / ("gitconfig-gh-" + slug)
                self.assertEqual(fragment.read_text(), f'[credential "https://github.com"]\n\tusername = "{login}"\n[credential "https://gist.github.com"]\n\tusername = "{login}"\n')

    def test_gh_credentials_idempotent_and_backed_up(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            config = home / ".gitconfig"
            original = b"[user]\r\n\tname = Example\r\n"
            config.write_bytes(original)
            machine = {"gh_accounts": {"default": "personal", "~/projects": "work"}}
            module.install_gh_credentials(installer, machine)
            first = config.read_bytes()
            self.assertTrue(first.startswith(original))
            self.assertNotIn(b"\n", first.replace(b"\r\n", b""))
            self.assertEqual((installer.backup_root / ".gitconfig").read_bytes(), original)
            module.install_gh_credentials(module.Installer(home, REPO, False), machine)
            self.assertEqual(config.read_bytes(), first)
            self.assertEqual(len(list((home / ".claude/local").glob("backup-*"))), 1)

    def test_gh_credentials_replace_block_in_place(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            prefix = b"# untouched prefix\r\n[user]\r\n\tname = Example\r\n"
            suffix = b"[alias]\r\n\ts = status\r\n# untouched suffix"
            config = home / ".gitconfig"
            config.write_bytes(prefix + b"# harness gh accounts begin\r\n# old block\r\n# harness gh accounts end\r\n" + suffix)
            module.install_gh_credentials(installer, {"gh_accounts": {"default": "new"}})
            result = config.read_bytes()
            self.assertTrue(result.startswith(prefix))
            self.assertTrue(result.endswith(suffix))
            self.assertNotIn(b"# old block", result)
            self.assertEqual(result.count(b"# harness gh accounts begin"), 1)

    def test_gh_credentials_reset_and_reuse_existing_helper(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            original = b'[credential "https://github.com"]\n\thelper = !gh auth git-credential\n[credential "https://gist.github.com"]\n\thelper = !gh auth git-credential\n'
            config = home / ".gitconfig"
            config.write_bytes(original)
            module.install_gh_credentials(installer, {"gh_account": "personal"})
            result = config.read_bytes()
            self.assertTrue(result.startswith(original))
            self.assertEqual(result.count(b"!gh auth git-credential"), 4)
            self.assertIn(b'https://github.com"]\n\thelper =\n', result)
            self.assertEqual(self.gh_config(home, "credential.https://github.com.username").stdout.strip(), "personal")

    def test_gh_credentials_remove_block_and_fragments(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            config = home / ".gitconfig"
            original = b"# user content\n"
            config.write_bytes(original)
            module.install_gh_credentials(installer, {"gh_accounts": {"default": "personal", "~/projects": "work"}})
            module.install_gh_credentials(installer, {})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(list((home / ".claude/local").glob("gitconfig-gh-*")), [])

    def test_gh_credentials_dry_run_writes_nothing(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            installer.dry_run = True
            with contextlib.redirect_stdout(io.StringIO()) as output:
                module.install_gh_credentials(installer, {"gh_accounts": {"default": "personal", "~/projects": "work"}})
            self.assertIn("would write", output.getvalue())
            self.assertEqual(list(home.iterdir()), [])

    def test_gh_credentials_reuse_quoted_and_bare_helpers(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            windows_line = r"    helper = !'C:\\Program Files\\GitHub CLI\\gh.exe' auth git-credential"
            bare_line = "\thelper = !'/opt/GitHub CLI/gh' auth git-credential"
            original = ('[credential]\n' + bare_line + '\n[credential "https://github.com"]\n' + windows_line + '\n').encode()
            config = home / ".gitconfig"
            config.write_bytes(original)
            module.install_gh_credentials(installer, {"gh_account": "personal"})
            first = config.read_bytes()
            self.assertTrue(first.startswith(original))
            block = first[len(original):].decode()
            self.assertEqual(block.count(windows_line), 1)
            self.assertEqual(block.count(bare_line), 1)
            self.assertLess(block.index(windows_line), block.index('[credential "https://gist.github.com"]'))
            self.assertIn("# The installer rewrites this block on every run; re-run install.py after a Python upgrade.", block)
            module.install_gh_credentials(installer, {"gh_account": "personal"})
            self.assertEqual(config.read_bytes(), first)

    def test_gh_credentials_placeholders_preserve_configuration(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            config = home / ".gitconfig"
            config.write_bytes(b"# keep this content\n")
            module.install_gh_credentials(installer, {"gh_accounts": {"default": "valid", "~/projects": "work"}})
            original = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
            for invalid in ("TODO", "TODO-sample-handle", "ToDo", "bad_name", "bad login", "-start", "end-", "a" * 40):
                for machine in (
                    {"gh_accounts": {"default": invalid}},
                    {"gh_account": invalid},
                    {"gh_accounts": {"~/projects": "valid"}, "gh_account": invalid},
                    {"gh_accounts": {"default": "valid", "~/projects": invalid}},
                ):
                    with self.subTest(machine=machine), contextlib.redirect_stdout(io.StringIO()) as output:
                        module.validate_gh_accounts(machine, lambda key: "probe.json")
                        module.install_gh_credentials(installer, machine)
                        self.assertIn(f"skipped (gh account {invalid} is a placeholder)", output.getvalue())
                        self.assertEqual({path: path.read_bytes() for path in home.rglob("*") if path.is_file()}, original)

    def test_gh_credentials_default_fallback_and_agreement(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            for accounts in ({"~/projects": "work"}, {"default": "personal", "~/projects": "work"}):
                machine = {"gh_accounts": accounts, "gh_account": "personal"}
                module.validate_gh_accounts(machine, lambda key: "probe.json")
                module.install_gh_credentials(installer, machine)
                self.assertEqual(self.gh_config(home, "credential.https://github.com.username").stdout.strip(), "personal")
                self.assertEqual(machine["gh_accounts"], accounts)
            with self.assertRaisesRegex(ValueError, "gh_accounts.default.*gh_account"):
                module.validate_gh_accounts({"gh_accounts": {"default": "personal"}, "gh_account": "other"}, lambda key: "probe.json")

    def test_gh_credentials_removal_requires_absent_or_null_keys(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            config = home / ".gitconfig"
            original = b"# preserved\n"
            config.write_bytes(original)
            for machine in ({}, {"gh_accounts": None}, {"gh_account": None}, {"gh_accounts": None, "gh_account": None}):
                module.install_gh_credentials(installer, {"gh_accounts": {"default": "personal", "~/projects": "work"}})
                installed = config.read_bytes()
                fragments = {path: path.read_bytes() for path in (home / ".claude/local").glob("gitconfig-gh-*")}
                module.install_gh_credentials(installer, {"gh_accounts": {}})
                self.assertEqual(config.read_bytes(), installed)
                self.assertEqual({path: path.read_bytes() for path in fragments}, fragments)
                module.install_gh_credentials(installer, machine)
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual(list((home / ".claude/local").glob("gitconfig-gh-*")), [])
            module.install_gh_credentials(installer, {"gh_accounts": None, "gh_account": "personal"})
            self.assertEqual(self.gh_config(home, "credential.https://github.com.username").stdout.strip(), "personal")

    def test_gh_credentials_valid_login_boundaries(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            for login in ("a", "9", "a-b", "a" * 39):
                with self.subTest(login=login), contextlib.redirect_stdout(io.StringIO()) as output:
                    module.install_gh_credentials(installer, {"gh_account": login})
                    self.assertNotIn("skipped", output.getvalue())
                    self.assertEqual(self.gh_config(home, "credential.https://github.com.username").stdout.strip(), login)

    def test_gh_accounts_reject_relative_prefixes(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            for prefix in ("relative", "./repo", "C:relative", "~alex/work", "~other"):
                with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, re.escape(prefix)):
                    module.validate_gh_accounts({"gh_accounts": {prefix: "valid"}}, lambda key: "probe.json")
            for prefix in ("default", "~/repo", "~", "/srv/repo", "C:/repo", "C:\\repo"):
                module.validate_gh_accounts({"gh_accounts": {prefix: "valid"}}, lambda key: "probe.json")

    def test_install_with_placeholder_gh_login_completes(self):
        home = self.new_home("placeholder-gh-home")
        copied = self.copy_repo_without_local_machine_files("placeholder-gh-repo")
        local = home / ".claude/local/machine.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"gh_account": "TODO"}), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped (gh account TODO is a placeholder)", result.stdout)
        self.assertFalse((home / ".gitconfig").exists())

    def test_gh_accounts_validation(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in ([], "login", {"": "login"}, {"default": 123}, {"default": ""}, {"~/repo": None}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "gh_accounts"):
                module.validate_gh_accounts({"gh_accounts": value}, lambda key: "probe.json")
        for value in (123, "", " "):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "gh_account"):
                module.validate_gh_accounts({"gh_account": value}, lambda key: "probe.json")
        module.validate_gh_accounts({}, lambda key: "probe.json")
        module.validate_gh_accounts({"gh_accounts": {"default": "login", "~/repo": "other"}}, lambda key: "probe.json")

    def test_remote_skill_headless_guidance(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            installer = mock.Mock(home=Path(temporary), repo=REPO)
            module.install_remote_skills(installer, {"remote": {"probe": {"ssh": "probe-alias"}}}, REPO)
            text = installer.write_bytes.call_args.args[1].decode()
            sentence = "The tool passes a headless system prompt telling the remote session to keep long jobs in the foreground or under setsid nohup; `--no-preamble` skips it."
            self.assertEqual(text.count(sentence), 1)
            self.assertLess(text.index("3. Long prompts"), text.index(sentence))
            self.assertLess(text.index(sentence), text.index("4. Follow-ups"))

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name).resolve()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def new_home(self, name):
        path = self.base / name
        path.mkdir()
        return path

    def bundle_home(self, copied):
        return copied.with_name(copied.name + "-builder-home")

    def bundle_terms_file(self, copied):
        return self.bundle_home(copied) / ".claude" / "local" / "bundle-terms" / "probe.txt"

    def copy_repo_without_local_machine_files(self, name, client=None, terms=True):
        copied = self.base / name
        copied.mkdir()
        ignore_local = shutil.ignore_patterns("*.local.json", "*.local.md", "*.bundle-terms.txt", "__pycache__")
        for directory in (
            "machines",
            "claude",
            "templates",
            "codex",
            "audit",
            "checkers",
            "tools",
            "publish",
            "monitor",
        ):
            source = REPO / directory
            if source.exists():
                shutil.copytree(source, copied / directory, ignore=ignore_local)
        shutil.copy2(REPO / "install-manifest.json", copied / "install-manifest.json")
        shutil.copy2(REPO / "install.py", copied / "install.py")
        if (REPO / "harvest.py").is_file():
            shutil.copy2(REPO / "harvest.py", copied / "harvest.py")
        for machine_path in (copied / "machines").glob("*.json"):
            machine = read_json_test(machine_path)
            machine["owns"] = []
            if machine_path.stem == TEST_MACHINE and client is not None:
                machine["client"] = client
            machine_path.write_text(json.dumps(machine), encoding="utf-8")
        terms_path = self.bundle_terms_file(copied)
        terms_path.parent.mkdir(parents=True)
        if terms:
            terms_path.write_text(
                "bundleprobe" + " exclusion\n", encoding="utf-8"
            )
            self.declare_bundle_domains(copied, ["probe"])
        return copied

    def declare_bundle_domains(self, copied, domains):
        (copied / "machines" / "domain-owner.json").write_text(json.dumps({"client": False, "owns": domains}), encoding="utf-8")

    def initialize_bundle_git(self, copied):
        for arguments in (
            ["git", "init", "-q"],
            ["git", "add", "machines"],
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=", "commit", "-qm", "Test fixture"],
        ):
            result = subprocess.run(arguments, cwd=copied, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def run_install(self, home, repo, extra=None):
        command = [
            sys.executable,
            str(REPO / "install.py"),
            "--machine",
            TEST_MACHINE,
            "--home",
            str(home),
            "--repo",
            str(repo),
            "--no-tests",
        ]
        if extra:
            command.extend(extra)
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def run_bundle(self, copied, output, home=None):
        return subprocess.run(
            [
                sys.executable,
                str(REPO / "install.py"),
                "--bundle",
                TEST_MACHINE,
                "--out",
                str(output),
                "--repo",
                str(copied),
                "--home",
                str(home if home is not None else self.bundle_home(copied)),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_empty_home_install(self):
        home = self.new_home("empty-home")
        copied = self.copy_repo_without_local_machine_files("empty-repo")
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (
            home / ".claude" / "CLAUDE.md",
            home / ".claude" / "hooks" / "secret_guard.py",
            home / ".claude" / "hooks" / "gh_credential_helper.py",
            home / ".claude" / "hooks" / "draft_wrap_guard.py",
            home / ".claude" / "hooks" / "context_save_nudge.py",
            home / ".claude" / "local" / "machine.json",
            home / ".claude" / "local" / "machine.md",
            home / ".claude" / "local" / "machine.local.md",
            home / ".claude" / "local" / "harness-mode",
            home / ".codex" / "AGENTS.md",
        ):
            self.assertTrue(path.is_file(), str(path))
        self.assertEqual(
            read_json_test(home / ".claude" / "keybindings.json"),
            read_json_test(copied / "claude" / "keybindings.json"),
        )
        settings = read_json_test(home / ".claude" / "settings.json")
        commands = [
            hook["command"]
            for entries in settings["hooks"].values()
            for entry in entries
            for hook in entry["hooks"]
        ]
        interpreter = str(Path(sys.executable).resolve()).replace("\\", "/")
        self.assertTrue(all(interpreter in command for command in commands))
        machine = read_json_test(copied / "machines" / f"{TEST_MACHINE}.json")
        self.assertTrue(
            set(machine["settings"]["permissions_deny"])
            <= set(settings["permissions"]["deny"])
        )
        self.assertEqual(settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "sonnet")
        self.assertEqual(settings["cleanupPeriodDays"], 90)
        self.assertEqual(settings["modelSettings"]["claude-fable-5-1"]["effortLevel"], "medium")
        installed_machine = read_json_test(home / ".claude" / "local" / "machine.json")
        self.assertEqual(installed_machine["codex_effort"], "high")
        self.assertEqual(installed_machine["claude_effort"], {"claude-fable-5-1": "medium", "claude-opus-5-5": "high", "claude-sonnet-5-5": "medium", "claude-haiku-5-5": "medium"})
        self.assertFalse((home / ".codex" / "config.toml").exists())
        self.assertIn(f'ACTION  {home / ".codex" / "config.toml"}  (skipped (no ~/.codex/config.toml))', result.stdout)
        self.assertNotIn("autoMode", settings)
        self.assertEqual(
            (home / ".claude" / "local" / "machine.local.md").read_text(
                encoding="utf-8"
            ),
            "# No local machine facts on this machine yet "
            f"(machines/{TEST_MACHINE}.local.md was not in the install folder; this file is yours, no install overwrites it).\n"
            "\n"
            "## Who I am, for calibration\n"
            "Not written yet. In Claude Code run /setup: it asks who you are, your git identities and gh account, your Codex model and the rules of your engagement, then writes this file and ~/.claude/local/machine.local.json.\n",
        )
        self.assertEqual(
            (home / ".claude" / "local" / "machine-name").read_text(encoding="utf-8").strip(),
            TEST_MACHINE,
        )
        self.assertEqual(
            (home / ".claude" / "local" / "harness-mode").read_text(
                encoding="utf-8"
            ),
            "bundle\n",
        )

    def test_install_writes_who_i_am_stub_when_local_markdown_missing(self):
        home = self.new_home("identity-stub-home")
        copied = self.copy_repo_without_local_machine_files("identity-stub-repo")
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        stub = (home / ".claude" / "local" / "machine.local.md").read_text(encoding="utf-8")
        self.assertIn("## Who I am, for calibration\n", stub)
        self.assertIn("Not written yet. In Claude Code run /setup:", stub)
        self.assertIn("then writes this file and ~/.claude/local/machine.local.json.\n", stub)
        self.assertIn(f"machines/{TEST_MACHINE}.local.md", stub)

    def test_copy_repo_without_harvest_tooling(self):
        copied = self.copy_repo_without_local_machine_files("copy-without-harvest-source")
        tool = copied / "harvest.py"
        if tool.is_file():
            tool.unlink()
        skill = copied / "claude" / "skills" / "harvest"
        self.assertTrue(skill.resolve().is_relative_to(self.base.resolve()))
        if skill.is_dir():
            shutil.rmtree(skill)
        with mock.patch.dict(globals(), REPO=copied):
            result = self.copy_repo_without_local_machine_files("copy-without-harvest-result")
        self.assertTrue((result / "install.py").is_file())
        self.assertFalse((result / "harvest.py").exists())
        self.assertFalse((result / "claude" / "skills" / "harvest").exists())

    def test_install_preserves_existing_local_markdown_without_source(self):
        home = self.new_home("identity-preserved-home")
        copied = self.copy_repo_without_local_machine_files("identity-preserved-repo")
        first = self.run_install(home, repo=copied)
        self.assertEqual(first.returncode, 0, first.stderr)
        destination = home / ".claude" / "local" / "machine.local.md"
        custom = b"## Who I am, for calibration\r\nCustom identity.\r\n"
        destination.write_bytes(custom)
        second = self.run_install(home, repo=copied)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(destination.read_bytes(), custom)
        self.assertIn(f"ACTION  {destination}  (unchanged)", second.stdout)

    def test_install_merges_filled_local_markdown_section(self):
        home = self.new_home("identity-merge-home")
        copied = self.copy_repo_without_local_machine_files("identity-merge-repo")
        source = copied / "machines" / f"{TEST_MACHINE}.local.md"
        supplied = "# Machine\n\n- Updated engagement facts.\n\n## Who I am, for calibration\nNot written yet.\n\n## Notes\nSupplied notes.\n"
        source.write_bytes(supplied.encode("utf-8-sig"))
        destination = home / ".claude" / "local" / "machine.local.md"
        destination.parent.mkdir(parents=True)
        personal = "## Who I am, for calibration\nI prefer concise explanations.\n### Details\nI review examples.\n\n"
        original = ("# Machine\n\n- Previous engagement facts.\n\n" + personal + "## Notes\nPrevious notes.\n").encode("utf-8-sig")
        destination.write_bytes(original)
        dry_run = self.run_install(home, copied, ["--dry-run"])
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr or dry_run.stdout)
        self.assertIn(f"ACTION  {destination}  (would write)", dry_run.stdout)
        self.assertEqual(destination.read_bytes(), original)
        self.assertFalse(list(destination.parent.glob("backup-*")))
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        expected = supplied.replace("## Who I am, for calibration\nNot written yet.\n\n", personal).encode("utf-8")
        self.assertEqual(destination.read_bytes(), expected)
        self.assertIn(f"ACTION  {destination}  (written)", result.stdout)
        backups = list(destination.parent.glob("backup-*/.claude/local/machine.local.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        repeated = self.run_install(home, copied)
        self.assertEqual(repeated.returncode, 0, repeated.stderr or repeated.stdout)
        self.assertIn(f"ACTION  {destination}  (unchanged)", repeated.stdout)
        self.assertEqual(destination.read_bytes(), expected)

    def test_install_overwrites_placeholder_local_markdown_section(self):
        home = self.new_home("identity-placeholder-home")
        copied = self.copy_repo_without_local_machine_files("identity-placeholder-repo")
        source = copied / "machines" / f"{TEST_MACHINE}.local.md"
        supplied = b"# Machine\n- Updated engagement facts.\n## Who I am, for calibration\nNot written yet.\n"
        source.write_bytes(supplied)
        destination = home / ".claude" / "local" / "machine.local.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"# Machine\n## Who I am, for calibration\nNot written yet. Add a paragraph.\n")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(destination.read_bytes(), supplied)

    def test_install_overwrites_local_markdown_with_filled_source(self):
        home = self.new_home("identity-filled-source-home")
        copied = self.copy_repo_without_local_machine_files("identity-filled-source-repo")
        source = copied / "machines" / f"{TEST_MACHINE}.local.md"
        supplied = b"# Machine\n- Updated engagement facts.\n## Who I am, for calibration\nI prefer examples.\n"
        source.write_bytes(supplied)
        destination = home / ".claude" / "local" / "machine.local.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"# Machine\n## Who I am, for calibration\nI prefer concise explanations.\n")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(destination.read_bytes(), supplied)

    def test_install_appends_personal_section_when_source_has_none(self):
        for index, ending in enumerate(("", "\n", "\n\n\n")):
            with self.subTest(ending=ending):
                home = self.new_home(f"identity-append-{index}-home")
                copied = self.copy_repo_without_local_machine_files(f"identity-append-{index}-repo")
                source = copied / "machines" / f"{TEST_MACHINE}.local.md"
                supplied = "# Machine\n- Updated engagement facts.\n\n## Notes\nSupplied notes."
                source.write_bytes((supplied + ending).encode("utf-8-sig"))
                destination = home / ".claude" / "local" / "machine.local.md"
                destination.parent.mkdir(parents=True)
                personal = "## Who I am, for calibration\nI prefer concise explanations.\n"
                destination.write_bytes(("# Machine\n- Previous facts.\n\n" + personal + "\n## Notes\nPrevious notes.\n").encode("utf-8-sig"))
                expected = (supplied + "\n\n" + personal + "\n").encode("utf-8")
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual(destination.read_bytes(), expected)
                repeated = self.run_install(home, copied)
                self.assertEqual(repeated.returncode, 0, repeated.stderr or repeated.stdout)
                self.assertEqual(destination.read_bytes(), expected)
                self.assertIn(f"ACTION  {destination}  (unchanged)", repeated.stdout)

    def test_install_replaces_alternate_supplied_placeholder_with_personal_section(self):
        home = self.new_home("identity-alternate-source-home")
        copied = self.copy_repo_without_local_machine_files("identity-alternate-source-repo")
        source = copied / "machines" / f"{TEST_MACHINE}.local.md"
        supplied = "# Machine\n- Updated engagement facts.\n## Who I am, for calibration\nReplace this paragraph with your own words.\n\n## Notes\nSupplied notes.\n"
        source.write_bytes(supplied.encode("utf-8"))
        destination = home / ".claude" / "local" / "machine.local.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"# Machine\n## Who I am, for calibration\nI prefer concise explanations.\n\n")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(destination.read_bytes(), supplied.replace("Replace this paragraph with your own words.", "I prefer concise explanations.").encode("utf-8"))

    def test_install_overwrites_alternate_destination_placeholder(self):
        home = self.new_home("identity-alternate-destination-home")
        copied = self.copy_repo_without_local_machine_files("identity-alternate-destination-repo")
        source = copied / "machines" / f"{TEST_MACHINE}.local.md"
        supplied = b"# Machine\n- Updated engagement facts.\n## Who I am, for calibration\nNot written yet.\n"
        source.write_bytes(supplied)
        destination = home / ".claude" / "local" / "machine.local.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"# Machine\n## Who I am, for calibration\nReplace this paragraph with your own words.\n")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(destination.read_bytes(), supplied)

    def test_install_local_markdown_merge_fallbacks_and_section_boundaries(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        home = self.new_home("identity-boundaries-home")
        source = home / "source.md"
        destination = home / "destination.md"
        heading = "## Who I am, for calibration\n"
        supplied = "# Machine\n- Supplied facts.\n" + heading + "Not written yet.\n"
        for existing in (None, "# Machine\nNo section.\n", "# No local machine facts on this machine yet\n" + heading + "I prefer examples.\n"):
            with self.subTest(existing=existing):
                source.write_bytes(supplied.encode("utf-8"))
                if existing is not None:
                    destination.write_bytes(existing.encode("utf-8"))
                with contextlib.redirect_stdout(io.StringIO()):
                    installer.install_local_markdown(installer.Installer(home, REPO, False), source, destination)
                self.assertEqual(destination.read_bytes(), supplied.encode("utf-8"))
        personal = heading + "I prefer examples."
        destination.write_bytes(personal.encode("utf-8"))
        source.write_bytes((supplied + "## Notes\nKeep these.\n").encode("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install_local_markdown(installer.Installer(home, REPO, False), source, destination)
        self.assertEqual(destination.read_bytes(), ("# Machine\n- Supplied facts.\n" + personal + "\n## Notes\nKeep these.\n").encode("utf-8"))

    def test_install_refreshes_old_local_markdown_stub(self):
        home = self.new_home("identity-old-stub-home")
        copied = self.copy_repo_without_local_machine_files("identity-old-stub-repo")
        destination = home / ".claude" / "local" / "machine.local.md"
        destination.parent.mkdir(parents=True)
        old_stub = (
            "# No local machine facts on this machine yet "
            f"(machines/{TEST_MACHINE}.local.md in the harness install folder; write this file by hand, no install overwrites it).\n"
            "\n## Who I am, for calibration\n"
            "Replace this paragraph with two to four sentences about yourself.\n"
        ).encode("utf-8")
        destination.write_bytes(old_stub)
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        stub = destination.read_text(encoding="utf-8")
        self.assertIn("Not written yet. In Claude Code run /setup:", stub)
        self.assertNotIn("Replace this paragraph", stub)
        backups = list(destination.parent.glob("backup-*/.claude/local/machine.local.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), old_stub)

    def test_install_refuses_local_markdown_directory(self):
        home = self.new_home("identity-directory-home")
        copied = self.copy_repo_without_local_machine_files("identity-directory-repo")
        destination = home / ".claude" / "local" / "machine.local.md"
        destination.mkdir(parents=True)
        marker = destination / "keep.txt"
        marker.write_bytes(b"keep")
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn(f"install refused: {destination}: must be a file", result.stdout)
        self.assertTrue(destination.is_dir())
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertFalse((destination.parent / "machine-name").exists())

    def test_install_reads_local_json_from_home_when_install_folder_has_none(self):
        home = self.new_home("home-local-json-home")
        copied = self.copy_repo_without_local_machine_files("home-local-json-repo")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.parent.mkdir(parents=True)
        local = {"gh_account": "someone", "gh_accounts": {"default": "someone", "~/projects": "other"}, "identities": {"default": "someone@example.invalid"}}
        local_path.write_text(json.dumps(local), encoding="utf-8")
        original = local_path.read_bytes()
        tracked = read_json_test(copied / "machines" / f"{TEST_MACHINE}.json")
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.run_install(home, repo=copied)
                self.assertEqual(result.returncode, 0, result.stderr)
                machine = read_json_test(home / ".claude" / "local" / "machine.json")
                self.assertEqual(machine["gh_account"], "someone")
                self.assertEqual(machine["gh_accounts"], local["gh_accounts"])
                self.assertEqual(machine["identities"]["default"], "someone@example.invalid")
                self.assertTrue(tracked.keys() <= machine.keys())
                self.assertEqual(machine["settings"], tracked["settings"])
                self.assertEqual(local_path.read_bytes(), original)

    def test_home_local_json_merges_over_install_folder_local_json(self):
        home = self.new_home("local-json-precedence-home")
        copied = self.copy_repo_without_local_machine_files("local-json-precedence-repo")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.parent.mkdir(parents=True)
        local_path.write_text(json.dumps({"gh_account": "someone", "identities": {"default": "someone@example.invalid"}}), encoding="utf-8")
        original = local_path.read_bytes()
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(
            json.dumps({"gh_account": "builder", "identities": {"project": "project@example.invalid"}}), encoding="utf-8"
        )
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        machine = read_json_test(home / ".claude" / "local" / "machine.json")
        self.assertEqual(machine["gh_account"], "someone")
        self.assertEqual(machine["identities"]["project"], "project@example.invalid")
        self.assertEqual(machine["identities"]["default"], "someone@example.invalid")
        note = f"note: {local_path} merged over machines/{TEST_MACHINE}.local.json"
        self.assertEqual(result.stdout.splitlines().count(note), 1)
        self.assertEqual(local_path.read_bytes(), original)

    def test_install_home_local_json_malformed_fails_clearly(self):
        home = self.new_home("home-local-json-malformed-home")
        copied = self.copy_repo_without_local_machine_files("home-local-json-malformed-repo")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.parent.mkdir(parents=True)
        local_path.write_text("{", encoding="utf-8")
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"install refused: {local_path}:", result.stdout)
        self.assertNotIn("usage:", result.stdout + result.stderr)
        self.assertFalse((local_path.parent / "machine-name").exists())
        self.assertEqual(list(home.rglob("*")), [home / ".claude", local_path.parent, local_path])
        self.assertEqual(local_path.read_bytes(), b"{")

    def test_install_folder_local_json_malformed_refuses_before_writes(self):
        home = self.new_home("folder-local-json-malformed-home")
        copied = self.copy_repo_without_local_machine_files("folder-local-json-malformed-repo")
        local_path = copied / "machines" / f"{TEST_MACHINE}.local.json"
        local_path.write_text("{", encoding="utf-8")
        home_local = home / ".claude" / "local" / "machine.local.json"
        home_local.parent.mkdir(parents=True)
        home_local.write_text("{}", encoding="utf-8")
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"install refused: {local_path}:", result.stdout)
        self.assertNotIn("usage:", result.stdout + result.stderr)
        self.assertEqual(list(home.rglob("*")), [home / ".claude", home_local.parent, home_local])

    def test_install_non_object_local_json_refuses_before_writes(self):
        for location in ("folder", "home"):
            for index, content in enumerate(("[]", '"text"', "null")):
                with self.subTest(location=location, content=content):
                    home = self.new_home(f"local-json-object-{location}-{index}-home")
                    copied = self.copy_repo_without_local_machine_files(f"local-json-object-{location}-{index}-repo")
                    local_path = (copied / "machines" / f"{TEST_MACHINE}.local.json" if location == "folder"
                                  else home / ".claude" / "local" / "machine.local.json")
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    local_path.write_text(content, encoding="utf-8")
                    before = sorted(home.rglob("*"))
                    result = self.run_install(home, repo=copied)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, f"install refused: {local_path}: must be a JSON object\n")
                    self.assertNotIn("usage:", result.stdout + result.stderr)
                    self.assertEqual(sorted(home.rglob("*")), before)
                    self.assertEqual(local_path.read_text(encoding="utf-8"), content)

    def test_merge_backup_and_idempotence(self):
        home = self.new_home("merge-home")
        copied = self.copy_repo_without_local_machine_files("merge-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["delete"] = []
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        seeded = {
            "foo": "preserved",
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "custom-stop"}]}]
            },
            "permissions": {"allow": ["Bash(codex *)", "Custom(rule)"]},
            "autoMode": {"soft_deny": ["custom line"]},
        }
        settings_path.write_text(json.dumps(seeded), encoding="utf-8")
        first = self.run_install(home, copied)
        self.assertEqual(first.returncode, 0, first.stderr)
        settings = read_json_test(settings_path)
        self.assertEqual(settings["foo"], "preserved")
        self.assertEqual(settings["hooks"]["Stop"], seeded["hooks"]["Stop"])
        self.assertIn("Custom(rule)", settings["permissions"]["allow"])
        self.assertEqual(settings["permissions"]["allow"].count("Bash(codex *)"), 1)
        self.assertEqual(settings["autoMode"]["soft_deny"], ["custom line"])
        backups = list((home / ".claude" / "local").glob("backup-*"))
        self.assertEqual(len(backups), 1)

        before_bytes = settings_path.read_bytes()
        before_tree = sorted(
            path.relative_to(home).as_posix() for path in home.rglob("*")
        )
        second = self.run_install(home, copied)
        self.assertEqual(second.returncode, 0, second.stderr)
        after_tree = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
        self.assertEqual(settings_path.read_bytes(), before_bytes)
        self.assertEqual(after_tree, before_tree)
        statuses = [line for line in second.stdout.splitlines() if line.startswith("ACTION")]
        self.assertTrue(statuses)
        self.assertTrue(all("(unchanged)" in line or "(skipped)" in line or "(skipped (no ~/.codex/config.toml))" in line for line in statuses))
        self.assertEqual(len(list((home / ".claude" / "local").glob("backup-*"))), 1)

    def test_statusline_default_install_and_replacement(self):
        harness = {"type": "command", "command": "bash ~/.claude/statusline-command.sh", "padding": 0}
        custom = {"command": "custom-status", "padding": 4}
        for index, (flag, seeded) in enumerate(((None, None), (True, custom), (True, {**harness, "padding": 4}))):
            with self.subTest(flag=flag, seeded=seeded):
                home = self.new_home(f"statusline-default-{index}-home")
                copied = self.copy_repo_without_local_machine_files(f"statusline-default-{index}-repo")
                machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                machine = read_json_test(machine_path)
                machine.pop("statusline", None)
                if flag is not None:
                    machine["statusline"] = flag
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                settings_path = home / ".claude" / "settings.json"
                if seeded is not None:
                    settings_path.parent.mkdir(parents=True)
                    settings_path.write_text(json.dumps({"statusLine": seeded}), encoding="utf-8")
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual((home / ".claude" / "statusline-command.sh").read_bytes(), (copied / "claude" / "statusline-command.sh").read_bytes())
                self.assertEqual(read_json_test(settings_path)["statusLine"], custom if seeded == custom else harness)
                if seeded == custom:
                    self.assertIn(f"ACTION  {settings_path}  (statusLine kept (existing custom status line))", result.stdout)

    def test_statusline_disable_removes_script_and_harness_setting(self):
        home = self.new_home("statusline-flip-home")
        copied = self.copy_repo_without_local_machine_files("statusline-flip-repo")
        first = self.run_install(home, copied)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        destination = home / ".claude" / "statusline-command.sh"
        self.assertTrue(destination.is_file())
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.write_text(json.dumps({"statusline": False}), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertFalse(destination.exists())
        self.assertNotIn("statusLine", read_json_test(home / ".claude" / "settings.json"))
        self.assertIn(f"ACTION  {destination}  (removed (statusline false))", result.stdout)

    def test_statusline_disable_preserves_foreign_setting(self):
        home = self.new_home("statusline-flip-custom-home")
        copied = self.copy_repo_without_local_machine_files("statusline-flip-custom-repo")
        first = self.run_install(home, copied)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        settings_path = home / ".claude" / "settings.json"
        settings = read_json_test(settings_path)
        custom = {"command": "custom-status", "padding": 4}
        settings["statusLine"] = custom
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.write_text(json.dumps({"statusline": False}), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertFalse((home / ".claude" / "statusline-command.sh").exists())
        self.assertEqual(read_json_test(settings_path)["statusLine"], custom)

    def test_statusline_disable_dry_run_preserves_script_and_settings(self):
        home = self.new_home("statusline-flip-dry-run-home")
        copied = self.copy_repo_without_local_machine_files("statusline-flip-dry-run-repo")
        first = self.run_install(home, copied)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        destination = home / ".claude" / "statusline-command.sh"
        original_script = destination.read_bytes()
        settings_path = home / ".claude" / "settings.json"
        original_settings = settings_path.read_bytes()
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.write_text(json.dumps({"statusline": False}), encoding="utf-8")
        result = self.run_install(home, copied, extra=["--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn(f"ACTION  {destination}  (would remove)", result.stdout)
        self.assertEqual(destination.read_bytes(), original_script)
        self.assertEqual(settings_path.read_bytes(), original_settings)

    def test_statusline_missing_source_preserves_harness_setting(self):
        home = self.new_home("statusline-missing-harness-home")
        copied = self.copy_repo_without_local_machine_files("statusline-missing-harness-repo")
        first = self.run_install(home, copied)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        settings_path = home / ".claude" / "settings.json"
        harness = read_json_test(settings_path)["statusLine"]
        (copied / "claude" / "statusline-command.sh").unlink()
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(read_json_test(settings_path)["statusLine"], harness)
        self.assertTrue((home / ".claude" / "statusline-command.sh").is_file())

    def test_statusline_second_install_is_unchanged(self):
        home = self.new_home("statusline-repeat-home")
        copied = self.copy_repo_without_local_machine_files("statusline-repeat-repo")
        first = self.run_install(home, copied)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        second = self.run_install(home, copied)
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        destination = home / ".claude" / "statusline-command.sh"
        self.assertIn(f"ACTION  {destination}  (unchanged)", second.stdout)

    def test_statusline_false_preserves_existing_setting(self):
        for present in (False, True):
            with self.subTest(present=present):
                home = self.new_home(f"statusline-disabled-{present}-home")
                copied = self.copy_repo_without_local_machine_files(f"statusline-disabled-{present}-repo")
                machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                machine = read_json_test(machine_path)
                machine["statusline"] = False
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                settings_path = home / ".claude" / "settings.json"
                settings_path.parent.mkdir(parents=True)
                foreign = {"type": "command", "command": "custom-status", "padding": 4}
                settings_path.write_text(json.dumps({"statusLine": foreign} if present else {}), encoding="utf-8")
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                settings = read_json_test(settings_path)
                self.assertEqual("statusLine" in settings, present)
                if present:
                    self.assertEqual(settings["statusLine"], foreign)
                self.assertFalse((home / ".claude" / "statusline-command.sh").exists())
                self.assertNotIn("statusline-command.sh", result.stdout)

    def test_statusline_invalid_flag_refuses_install_before_writes(self):
        for location in ("tracked", "folder", "home"):
            with self.subTest(location=location):
                home = self.new_home(f"statusline-invalid-{location}-home")
                copied = self.copy_repo_without_local_machine_files(f"statusline-invalid-{location}-repo")
                path = copied / "machines" / f"{TEST_MACHINE}.json"
                if location == "folder":
                    path = path.with_name(f"{TEST_MACHINE}.local.json")
                elif location == "home":
                    path = home / ".claude" / "local" / "machine.local.json"
                machine = read_json_test(path) if path.is_file() else {}
                machine["statusline"] = "yes"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(machine), encoding="utf-8")
                before = sorted(home.rglob("*"))
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertEqual(result.stdout, f"install refused: {path}: statusline must be true or false\n")
                self.assertEqual(sorted(home.rglob("*")), before)
                self.assertNotIn("ACTION ", result.stdout)

    def copy_codex_effort_repo(self, name):
        copied = self.copy_repo_without_local_machine_files(name)
        path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(path)
        machine.pop("codex_model", None)
        path.write_text(json.dumps(machine), encoding="utf-8")
        return copied

    @contextlib.contextmanager
    def codex_config_fixture(self, name, original, dry_run=False):
        home = self.new_home(f"codex-{name}-home")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_bytes(original)
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        installer = module.Installer(home, REPO, dry_run)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            yield module, installer, config, output

    def test_codex_model_preserves_other_bytes(self):
        for index, original in enumerate((
            b'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n# keep\n[profile]\nmodel = "nested"',
            b"\xef\xbb\xbf# keep\r\nmodel\t= 'gpt-6-astra'  # keep\r\n[profile]\r\nmodel = 'nested'\r\n",
            b'model = "gpt-6-astra"',
        )):
            with self.subTest(index=index), self.codex_config_fixture(f"model-bytes-{index}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
                self.assertEqual(config.read_bytes(), original.replace(b"gpt-6-astra", b"gpt-6.1-sol"))
                self.assertEqual(output.getvalue(), f"ACTION  {config}  (written)\n")
                self.assertEqual((installer.backup_root / ".codex/config.toml").read_bytes(), original)

    def test_codex_model_already_set_is_unchanged(self):
        original = b"\xef\xbb\xbfmodel  = 'gpt-6.1-sol' # keep\r\n"
        with self.codex_config_fixture("model-unchanged", original) as (module, installer, config, output):
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (unchanged)\n")
            self.assertIsNone(installer.backup_root)

    def test_codex_model_absent_is_inserted_first(self):
        for index, (original, expected) in enumerate((
            (b'model_reasoning_effort = "high"\n# keep\n', b'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n# keep\n'),
            (b'\xef\xbb\xbf# keep\r\nmodel_reasoning_effort = "high"\r\n', b'\xef\xbb\xbfmodel = "gpt-6.1-sol"\r\n# keep\r\nmodel_reasoning_effort = "high"\r\n'),
            (b'# keep\n[profile]\nkeep = true', b'model = "gpt-6.1-sol"\n# keep\n[profile]\nkeep = true'),
            (b'notes = """\nkeep\n"""\n', b'model = "gpt-6.1-sol"\nnotes = """\nkeep\n"""\n'),
            (b'model_reasoning_effort = "high"', b'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"'),
            (b'', b'model = "gpt-6.1-sol"\n'),
        )):
            with self.subTest(index=index), self.codex_config_fixture(f"model-insert-{index}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
                self.assertEqual(config.read_bytes(), expected)
                self.assertEqual(output.getvalue(), f"ACTION  {config}  (written)\n")

    def test_codex_model_and_effort_absent_are_inserted_in_order(self):
        original = b"\xef\xbb\xbf# keep\r\n[profile]\r\nkeep = true"
        with self.codex_config_fixture("model-effort-insert", original) as (module, installer, config, output):
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            self.assertEqual(config.read_bytes(), b'\xef\xbb\xbfmodel = "gpt-6.1-sol"\r\nmodel_reasoning_effort = "high"\r\n' + original[3:])
            self.assertEqual(output.getvalue().splitlines(), [f"ACTION  {config}  (written)"])

    def assert_codex_model_skipped(self, name, original, reason):
        with self.codex_config_fixture(f"model-skip-{name}", original) as (module, installer, config, output):
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (skipped ({reason}))\n")
            self.assertIsNone(installer.backup_root)

    def test_codex_model_indented_key_is_skipped(self):
        self.assert_codex_model_skipped("indented", b'  model = "gpt-6-astra"\n',
                                       "model present in a form the installer does not rewrite")

    def test_codex_model_quoted_key_is_skipped(self):
        for index, original in enumerate((b'"model" = "gpt-6-astra"\n', b"'model' = 'gpt-6-astra'\n")):
            with self.subTest(index=index):
                self.assert_codex_model_skipped(f"quoted-{index}", original,
                                               "model present in a form the installer does not rewrite")

    def test_codex_model_in_table_does_not_block_top_level_insertion(self):
        for index, original in enumerate((
            b'[profiles.fast]\nmodel = "x"\n',
            b'[profile]\nmodel = "gpt-6-astra"\n',
            b'profile = { model = "gpt-6-astra" }\n',
            b'[[profiles]]\nmodel = "gpt-6-astra"\n',
            b'profile.nested.model = "gpt-6-astra"\n',
        )):
            with self.subTest(index=index), self.codex_config_fixture(f"model-table-{index}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
                self.assertEqual(config.read_bytes(), b'model = "gpt-6.1-sol"\n' + original)
                self.assertEqual(output.getvalue(), f"ACTION  {config}  (written)\n")

    def test_codex_model_after_multiline_string_is_skipped(self):
        for index, original in enumerate((
            b'notes = """\n[Rules]\nkeep\n"""\nmodel = "gpt-6-astra"\n',
            b'notes = """\nkeep\n"""\nmodel = "gpt-6-astra"\n',
            b"notes = '''\nkeep\n'''\nmodel = 'gpt-6-astra'\n",
            b'notes = """\nmodel = "inside string"\n"""\nmodel = "gpt-6-astra"\n',
        )):
            with self.subTest(index=index):
                self.assert_codex_model_skipped(f"multiline-{index}", original,
                                               "model present in a form the installer does not rewrite")

    def test_codex_model_triple_quotes_in_comments_or_single_line_strings_allow_rewrite(self):
        for index, prefix in enumerate((b'# """\n', b"# '''\n", b"notes = \"'''\"\n", b"notes = '\"\"\"'\n")):
            original = prefix + b'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n'
            with self.subTest(index=index), self.codex_config_fixture(f"model-comment-quotes-{index}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
                self.assertEqual(config.read_bytes(), original.replace(b"gpt-6-astra", b"gpt-6.1-sol").replace(b'"medium"', b'"high"'))
                self.assertEqual(output.getvalue(), f"ACTION  {config}  (written)\n")

    def test_codex_multiline_key_values_do_not_expose_partial_assignments(self):
        import tomllib
        for key, desired, original, inserted in (
            ("model", "gpt-6-astra", b'model = """gpt-6-astra"""\n# keep\n', b'model_reasoning_effort = "high"\n'),
            ("model_reasoning_effort", "medium", b'model_reasoning_effort = """medium"""\n# keep\n', b'model = "gpt-6.1-sol"\n'),
        ):
            with self.subTest(key=key), self.codex_config_fixture(f"multiline-value-{key}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
                self.assertEqual(config.read_bytes(), inserted + original)
                self.assertEqual(tomllib.loads(config.read_bytes().decode("utf-8"))[key], desired)
                self.assertEqual(output.getvalue().splitlines(), [
                    f"ACTION  {config}  (skipped ({key} present in a form the installer does not rewrite))",
                    f"ACTION  {config}  (written)",
                ])

    def test_codex_model_invalid_toml_is_skipped(self):
        for index, original in enumerate((b'model = "gpt-6-astra"\n[broken', b'model = "old"\nmodel = "duplicate"\n', b'model = "\xff"\n')):
            with self.subTest(index=index):
                self.assert_codex_model_skipped(f"invalid-{index}", original, "config.toml did not parse")

    def test_codex_model_unrecognised_value_is_skipped(self):
        self.assert_codex_model_skipped("numeric", b'model = 123\n',
                                       "model present in a form the installer does not rewrite")

    def test_codex_model_absent_or_null_machine_value_prints_nothing(self):
        original = b'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n'
        for index, machine in enumerate(({"codex_effort": None}, {"codex_model": None, "codex_effort": None})):
            with self.subTest(index=index), self.codex_config_fixture(f"model-disabled-{index}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, machine)
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual(output.getvalue(), "")
                self.assertIsNone(installer.backup_root)

    def test_codex_model_and_effort_change_in_one_write(self):
        original = b'\xef\xbb\xbfmodel = "gpt-6-astra" # keep\r\nmodel_reasoning_effort = "medium"\r\n[profile]\r\nkeep = true'
        with self.codex_config_fixture("model-effort-write", original) as (module, installer, config, output):
            with mock.patch.object(installer, "write_bytes", wraps=installer.write_bytes) as writer:
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            expected = original.replace(b"gpt-6-astra", b"gpt-6.1-sol").replace(b'"medium"', b'"high"')
            writer.assert_called_once_with(config, expected)
            self.assertEqual(config.read_bytes(), expected)
            self.assertEqual(output.getvalue().splitlines(), [f"ACTION  {config}  (written)"])
            self.assertEqual((installer.backup_root / ".codex/config.toml").read_bytes(), original)

    def test_codex_model_and_effort_unchanged_report_once(self):
        original = b'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n'
        with self.codex_config_fixture("model-effort-unchanged", original) as (module, installer, config, output):
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (unchanged)\n")

    def test_codex_model_and_effort_missing_file_report_once(self):
        home = self.new_home("codex-model-effort-missing-home")
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        installer = module.Installer(home, REPO, False)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
        config = home / ".codex/config.toml"
        self.assertFalse(config.exists())
        self.assertEqual(output.getvalue(), f"ACTION  {config}  (skipped (no ~/.codex/config.toml))\n")

    def test_codex_model_dry_run_writes_nothing(self):
        original = b'model = "gpt-6-astra"\r\n# keep\r\n'
        with self.codex_config_fixture("model-dry", original, dry_run=True) as (module, installer, config, output):
            before = sorted(installer.home.rglob("*"))
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(sorted(installer.home.rglob("*")), before)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (would write)\n")

    def test_codex_model_and_effort_dry_run_writes_nothing(self):
        original = b'model = "gpt-6-astra"\r\nmodel_reasoning_effort = "medium"\r\n'
        with self.codex_config_fixture("model-effort-dry", original, dry_run=True) as (module, installer, config, output):
            before = sorted(installer.home.rglob("*"))
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(sorted(installer.home.rglob("*")), before)
            self.assertEqual(output.getvalue().splitlines(), [f"ACTION  {config}  (would write)"])

    def test_codex_model_without_tomllib_skips_insertion(self):
        original = b'model_reasoning_effort = "high"\n# \xff\n'
        with self.codex_config_fixture("model-no-tomllib", original) as (module, installer, config, output):
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (skipped (no tomllib to confirm the key is absent))\n")

    def test_codex_model_without_tomllib_replaces_recognised_key(self):
        original = b'model = "gpt-6-astra" # keep\r\n# \xff\r\n'
        with self.codex_config_fixture("model-no-tomllib-replace", original) as (module, installer, config, output):
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": None})
            self.assertEqual(config.read_bytes(), original.replace(b"gpt-6-astra", b"gpt-6.1-sol"))
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (written)\n")

    def test_codex_model_skip_does_not_block_effort(self):
        original = b'  model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n'
        with self.codex_config_fixture("model-skip-effort-write", original) as (module, installer, config, output):
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            self.assertEqual(config.read_bytes(), original.replace(b'"medium"', b'"high"'))
            self.assertEqual(output.getvalue().splitlines(), [
                f"ACTION  {config}  (skipped (model present in a form the installer does not rewrite))",
                f"ACTION  {config}  (written)",
            ])

    def test_codex_model_write_omits_unchanged_effort_action(self):
        original = b'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n'
        with self.codex_config_fixture("model-write-effort-unchanged", original) as (module, installer, config, output):
            module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            self.assertEqual(config.read_bytes(), original.replace(b"gpt-6-astra", b"gpt-6.1-sol"))
            self.assertEqual(output.getvalue().splitlines(), [f"ACTION  {config}  (written)"])

    def test_codex_model_skipped_and_effort_unchanged_report_only_skip(self):
        original = b'  model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n'
        with self.codex_config_fixture("model-skipped-effort-unchanged", original) as (module, installer, config, output):
            with mock.patch.object(installer, "write_bytes", wraps=installer.write_bytes) as writer:
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            writer.assert_not_called()
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (skipped (model present in a form the installer does not rewrite))\n")

    def test_codex_model_changed_and_effort_skipped_report_skip_before_write(self):
        original = b'model = "gpt-6-astra"\n  model_reasoning_effort = "medium"\n'
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.codex_config_fixture(f"model-write-effort-skipped-{dry_run}", original, dry_run=dry_run) as (module, installer, config, output):
                with mock.patch.object(installer, "write_bytes", wraps=installer.write_bytes) as writer:
                    module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
                expected = original.replace(b"gpt-6-astra", b"gpt-6.1-sol")
                writer.assert_called_once_with(config, expected)
                self.assertEqual(config.read_bytes(), original if dry_run else expected)
                status = "would write" if dry_run else "written"
                self.assertEqual(output.getvalue().splitlines(), [
                    f"ACTION  {config}  (skipped (model_reasoning_effort present in a form the installer does not rewrite))",
                    f"ACTION  {config}  ({status})",
                ])

    def test_codex_model_and_effort_skips_are_reported_in_key_order(self):
        original = b'  model = "gpt-6-astra"\n  model_reasoning_effort = "medium"\n'
        with self.codex_config_fixture("model-effort-skipped", original) as (module, installer, config, output):
            with mock.patch.object(installer, "write_bytes", wraps=installer.write_bytes) as writer:
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            writer.assert_not_called()
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue().splitlines(), [
                f"ACTION  {config}  (skipped (model present in a form the installer does not rewrite))",
                f"ACTION  {config}  (skipped (model_reasoning_effort present in a form the installer does not rewrite))",
            ])

    def test_codex_model_and_effort_parse_failure_is_reported_once(self):
        original = b'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n[broken'
        with self.codex_config_fixture("model-effort-parse-failure", original) as (module, installer, config, output):
            with mock.patch.object(installer, "write_bytes", wraps=installer.write_bytes) as writer:
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            writer.assert_not_called()
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (skipped (config.toml did not parse))\n")

    def test_codex_model_and_effort_parse_once(self):
        import tomllib
        original = b'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n'
        with self.codex_config_fixture("model-effort-parse-once", original) as (module, installer, config, output):
            with mock.patch.object(tomllib, "loads", wraps=tomllib.loads) as parser:
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            parser.assert_called_once_with(original.decode("utf-8-sig"))
            self.assertEqual(config.read_bytes(), original.replace(b"gpt-6-astra", b"gpt-6.1-sol").replace(b'"medium"', b'"high"'))
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (written)\n")

    def test_codex_model_failed_write_does_not_drop_effort_skip_or_print_written(self):
        original = b'model = "gpt-6-astra"\n  model_reasoning_effort = "medium"\n'
        with self.codex_config_fixture("model-write-false-effort-skipped", original) as (module, installer, config, output):
            with mock.patch.object(installer, "write_bytes", return_value=False) as writer:
                module.install_codex_effort(installer, {"codex_model": "gpt-6.1-sol", "codex_effort": "high"})
            writer.assert_called_once_with(config, original.replace(b"gpt-6-astra", b"gpt-6.1-sol"))
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(output.getvalue(), f"ACTION  {config}  (skipped (model_reasoning_effort present in a form the installer does not rewrite))\n")

    def test_codex_model_and_effort_use_merged_machine_json(self):
        copied = self.copy_repo_without_local_machine_files("model-merged-repo")
        home = self.new_home("model-merged-home")
        local = home / ".claude/local/machine.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"codex_model": "gpt-6-astra", "codex_effort": "low"}), encoding="utf-8")
        config = home / ".codex/config.toml"
        config.parent.mkdir()
        original = b'model = "gpt-6.1-sol" # keep\r\nmodel_reasoning_effort = "medium"\r\n[profile]\r\nkeep = true'
        config.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), original.replace(b"gpt-6.1-sol", b"gpt-6-astra").replace(b'"medium"', b'"low"'))
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith(f"ACTION  {config}  ")], [f"ACTION  {config}  (written)"])
        backups = list((home / ".claude/local").glob("backup-*/.codex/config.toml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_codex_model_absent_or_null_machine_json_leaves_model_untouched(self):
        copied = self.copy_codex_effort_repo("model-disabled-repo")
        original = b'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n'
        for index, override in enumerate(({}, {"codex_model": None})):
            with self.subTest(index=index):
                home = self.new_home(f"model-disabled-json-{index}-home")
                local = home / ".claude/local/machine.local.json"
                local.parent.mkdir(parents=True)
                local.write_text(json.dumps(override), encoding="utf-8")
                config = home / ".codex/config.toml"
                config.parent.mkdir()
                config.write_bytes(original)
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual([line for line in result.stdout.splitlines() if line.startswith(f"ACTION  {config}  ")], [f"ACTION  {config}  (unchanged)"])

    def test_codex_model_values_are_toml_safe(self):
        import tomllib
        for index, (original, desired) in enumerate((
            (b'model = "old"\n', 'test"model\\value\n\x7f'),
            (b'', 'test"model\\value\n\x7f'),
            (b'model = "old"\n', "test" + "".join(chr(codepoint) for codepoint in range(32)) + "\x7f\u00e9"),
            (b"model = 'old'\n", 'test\\model'),
        )):
            with self.subTest(index=index), self.codex_config_fixture(f"model-safe-{index}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, {"codex_model": desired, "codex_effort": None})
                parsed = tomllib.loads(config.read_bytes().decode("utf-8-sig"))
                self.assertEqual(parsed["model"], desired)
                self.assertEqual(output.getvalue(), f"ACTION  {config}  (written)\n")

    def test_codex_model_unsafe_literal_value_is_skipped(self):
        original = b"model = 'old'\n"
        for index, desired in enumerate(("test'model", "test\nmodel", "test\x7fmodel")):
            with self.subTest(index=index), self.codex_config_fixture(f"model-unsafe-{index}", original) as (module, installer, config, output):
                module.install_codex_effort(installer, {"codex_model": desired, "codex_effort": None})
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual(output.getvalue(), f"ACTION  {config}  (skipped (model present in a form the installer does not rewrite))\n")

    def test_codex_effort_preserves_other_bytes(self):
        copied = self.copy_codex_effort_repo("effort-bytes-repo")
        for index, (original, expected) in enumerate((
            (b'\xef\xbb\xbfmodel = "test"\r\nmodel_reasoning_effort = "medium"\r\n# keep\r\n[profile]\r\nmodel_reasoning_effort = "high"\r\n',
             b'\xef\xbb\xbfmodel = "test"\r\nmodel_reasoning_effort = "high"\r\n# keep\r\n[profile]\r\nmodel_reasoning_effort = "high"\r\n'),
            (b'model = "test"\n# keep\n[profile]\nmodel_reasoning_effort = "high"',
             b'model = "test"\nmodel_reasoning_effort = "high"\n# keep\n[profile]\nmodel_reasoning_effort = "high"'),
            (b'\xef\xbb\xbf# keep\r\n[profile]\r\nmodel = "test"\r\n',
             b'\xef\xbb\xbfmodel_reasoning_effort = "high"\r\n# keep\r\n[profile]\r\nmodel = "test"\r\n'),
            (b'model = "test"', b'model = "test"\nmodel_reasoning_effort = "high"\n'),
            (b'', b'model_reasoning_effort = "high"\n'),
        )):
            with self.subTest(index=index):
                home = self.new_home(f"effort-bytes-{index}-home")
                config = home / ".codex" / "config.toml"
                config.parent.mkdir()
                config.write_bytes(original)
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertEqual(config.read_bytes(), expected)
                self.assertIn(f"ACTION  {config}  (written)", result.stdout)
                backups = list((home / ".claude" / "local").glob("backup-*/.codex/config.toml"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), original)

    def test_effort_explicit_values_preserve_other_model_settings(self):
        home = self.new_home("effort-explicit-home")
        copied = self.copy_codex_effort_repo("effort-explicit-repo")
        local = home / ".claude" / "local" / "machine.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"codex_effort": "low", "claude_effort": {"claude-fable-5-1": "high"}}), encoding="utf-8")
        settings_path = home / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"modelSettings": {
            "claude-fable-5-1": {"effortLevel": "low", "keep": True},
            "claude-opus-5-5": {"effortLevel": "high", "keep": "value"},
        }}), encoding="utf-8")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_bytes(b'model_reasoning_effort = "high"\n')
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), b'model_reasoning_effort = "low"\n')
        self.assertEqual(read_json_test(settings_path)["modelSettings"], {
            "claude-fable-5-1": {"effortLevel": "high", "keep": True},
            "claude-opus-5-5": {"effortLevel": "high", "keep": "value"},
        })

    def test_effort_invalid_values_refuse_before_writes(self):
        copied = self.copy_repo_without_local_machine_files("effort-invalid-repo")
        cases = (("codex_effort", "max"), ("codex_effort", []), ("claude_effort", "high"),
                 ("claude_effort", {"model": "max"}), ("claude_effort", {"model": None}),
                 ("claude_effort", {"model": []}))
        for index, (key, value) in enumerate(cases):
            with self.subTest(key=key, value=value):
                home = self.new_home(f"effort-invalid-{index}-home")
                local = home / ".claude" / "local" / "machine.local.json"
                local.parent.mkdir(parents=True)
                local.write_text(json.dumps({key: value}), encoding="utf-8")
                before = {str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}
                paths = sorted(home.rglob("*"))
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn(key, result.stdout)
                for allowed in ("low", "medium", "high", "xhigh"):
                    self.assertIn(allowed, result.stdout)
                self.assertNotIn("ACTION ", result.stdout)
                self.assertEqual(sorted(home.rglob("*")), paths)
                self.assertEqual({str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}, before)

    def test_muse_effort_invalid_values_refused(self):
        copied = self.copy_repo_without_local_machine_files("muse-effort-invalid-repo")
        for index, value in enumerate(("ultra", "", [], {}, 123, False)):
            with self.subTest(value=value):
                home = self.new_home(f"muse-effort-invalid-{index}-home")
                local = home / ".claude" / "local" / "machine.local.json"
                local.parent.mkdir(parents=True)
                local.write_text(json.dumps({"muse_effort": value}), encoding="utf-8")
                before = {str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}
                paths = sorted(home.rglob("*"))
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn(f"{local}: muse_effort must be minimal, low, medium, high, xhigh, max or null", result.stdout)
                self.assertNotIn("ACTION ", result.stdout)
                self.assertEqual(sorted(home.rglob("*")), paths)
                self.assertEqual({str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}, before)

    def test_muse_model_invalid_values_refused(self):
        copied = self.copy_repo_without_local_machine_files("muse-model-invalid-repo")
        for index, value in enumerate(("", " ", [], {}, 123, False, "muse&echo", "muse|echo", "-muse", "muse\n", "muse/model")):
            with self.subTest(value=value):
                home = self.new_home(f"muse-model-invalid-{index}-home")
                local = home / ".claude" / "local" / "machine.local.json"
                local.parent.mkdir(parents=True)
                local.write_text(json.dumps({"muse_model": value}), encoding="utf-8")
                before = {str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}
                paths = sorted(home.rglob("*"))
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn(f"{local}: muse_model must match [A-Za-z0-9][A-Za-z0-9._-]* or be null", result.stdout)
                self.assertNotIn("ACTION ", result.stdout)
                self.assertEqual(sorted(home.rglob("*")), paths)
                self.assertEqual({str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}, before)

    def test_kimi_model_validation(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            source = home / ".claude" / "local" / "machine.local.json"
            for value in (None, "kimi-code/kimi-for-coding", "Model_1.0-test", "0"):
                with self.subTest(value=value):
                    module.validate_machine_efforts({"kimi_model": value}, lambda key: source)
            for value in ("", " ", [], {}, 123, False, "kimi&echo", "kimi|echo", "-kimi", "kimi\n", " kimi"):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError) as caught:
                        module.validate_machine_efforts({"kimi_model": value}, lambda key: source)
                    self.assertEqual(str(caught.exception), f"{source}: kimi_model must match [A-Za-z0-9][A-Za-z0-9._/-]* or be null")

    def test_kimi_effort_validation(self):
        with self.gh_credentials_fixture() as (module, installer, home):
            source = home / ".claude" / "local" / "machine.local.json"
            for value in (None, "low", "high", "max"):
                with self.subTest(value=value):
                    module.validate_machine_efforts({"kimi_effort": value}, lambda key: source)
            for value in ("", " ", "medium", "LOW", 1, [], {}):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError) as caught:
                        module.validate_machine_efforts({"kimi_effort": value}, lambda key: source)
                    self.assertEqual(str(caught.exception), f"{source}: kimi_effort must be low, high, max or null")

    def test_effort_null_disables_changes(self):
        home = self.new_home("effort-null-home")
        copied = self.copy_codex_effort_repo("effort-null-repo")
        local = home / ".claude" / "local" / "machine.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"codex_effort": None, "claude_effort": None}), encoding="utf-8")
        models = {"claude-opus-5-5": {"effortLevel": "high", "keep": True}}
        settings_path = home / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"modelSettings": models}), encoding="utf-8")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = b'\xef\xbb\xbfmodel_reasoning_effort = "high"\r\n'
        config.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), original)
        self.assertNotIn(f"ACTION  {config} ", result.stdout)
        self.assertEqual(read_json_test(settings_path)["modelSettings"], models)

    def test_effort_dry_run_writes_nothing(self):
        home = self.new_home("effort-dry-home")
        copied = self.copy_codex_effort_repo("effort-dry-repo")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = b'model_reasoning_effort = "medium"\r\n'
        config.write_bytes(original)
        paths = sorted(home.rglob("*"))
        result = self.run_install(home, copied, ["--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn(f"ACTION  {config}  (would write)", result.stdout)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(sorted(home.rglob("*")), paths)

    def test_codex_effort_already_set_is_unchanged(self):
        home = self.new_home("effort-unchanged-home")
        copied = self.copy_codex_effort_repo("effort-unchanged-repo")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = b"\xef\xbb\xbfmodel_reasoning_effort  = 'high' # keep\r\n"
        config.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), original)
        self.assertIn(f"ACTION  {config}  (unchanged)", result.stdout)
        self.assertFalse(list((home / ".claude" / "local").glob("backup-*/.codex/config.toml")))

    def assert_codex_effort_skipped(self, name, original, reason):
        home = self.new_home(f"effort-skip-{name}-home")
        copied = self.copy_codex_effort_repo(f"effort-skip-{name}-repo")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), original)
        self.assertIn(f"ACTION  {config}  (skipped ({reason}))", result.stdout)
        self.assertFalse(list((home / ".claude" / "local").glob("backup-*/.codex/config.toml")))

    @unittest.skipUnless(sys.version_info >= (3, 11), "requires tomllib")
    def test_codex_effort_indented_key_is_skipped(self):
        self.assert_codex_effort_skipped("indented", b'  model_reasoning_effort = "high"\n',
                                       "model_reasoning_effort present in a form the installer does not rewrite")

    @unittest.skipUnless(sys.version_info >= (3, 11), "requires tomllib")
    def test_codex_effort_quoted_key_is_skipped(self):
        self.assert_codex_effort_skipped("quoted", b'"model_reasoning_effort" = "high"\n',
                                       "model_reasoning_effort present in a form the installer does not rewrite")

    @unittest.skipUnless(sys.version_info >= (3, 11), "requires tomllib")
    def test_codex_effort_after_multiline_string_is_skipped(self):
        self.assert_codex_effort_skipped("multiline", b'notes = """\n[Rules]\nkeep\n"""\nmodel_reasoning_effort = "high"\n',
                                       "model_reasoning_effort present in a form the installer does not rewrite")

    @unittest.skipUnless(sys.version_info >= (3, 11), "requires tomllib")
    def test_codex_effort_after_multiline_string_without_table_header_is_skipped(self):
        self.assert_codex_effort_skipped("multiline-no-header", b'notes = """\nkeep\n"""\nmodel_reasoning_effort = "high"\n',
                                       "model_reasoning_effort present in a form the installer does not rewrite")

    @unittest.skipUnless(sys.version_info >= (3, 11), "requires tomllib")
    def test_codex_effort_invalid_toml_is_skipped(self):
        for index, original in enumerate((
            b'model_reasoning_effort = "high"\n[broken',
            b'model_reasoning_effort = "high"\nmodel_reasoning_effort = "low"\n',
            b'model = "test"\rmodel_reasoning_effort = "high"\r[profile]\rkeep = true',
            b'model = "\xff"\n',
        )):
            with self.subTest(index=index):
                self.assert_codex_effort_skipped(f"invalid-{index}", original, "config.toml did not parse")

    def test_codex_effort_preserves_inline_comment(self):
        home = self.new_home("effort-comment-home")
        copied = self.copy_codex_effort_repo("effort-comment-repo")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = b'model_reasoning_effort = "medium" # bumped for refactors\r\n'
        config.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), b'model_reasoning_effort = "high" # bumped for refactors\r\n')
        self.assertIn(f"ACTION  {config}  (written)", result.stdout)

    def test_codex_effort_without_tomllib_skips_insertion(self):
        home = self.new_home("effort-no-tomllib-home")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = b'model = "test"\n# \xff\n'
        config.write_bytes(original)
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        installer = mock.Mock(home=home)
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            module.install_codex_effort(installer, {"codex_effort": "medium"})
        installer.write_bytes.assert_not_called()
        installer.action.assert_called_once_with(config, "skipped (no tomllib to confirm the key is absent)")
        self.assertEqual(config.read_bytes(), original)

    def test_codex_effort_without_tomllib_skips_indented_key(self):
        home = self.new_home("effort-no-tomllib-indented-home")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        original = b'  model_reasoning_effort = "high"\r\n'
        config.write_bytes(original)
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        installer = module.Installer(home, REPO, False)
        with mock.patch.dict(sys.modules, {"tomllib": None}), contextlib.redirect_stdout(io.StringIO()) as output:
            module.install_codex_effort(installer, {"codex_effort": "medium"})
        self.assertIn(f"ACTION  {config}  (skipped (no tomllib to confirm the key is absent))", output.getvalue())
        self.assertEqual(config.read_bytes(), original)
        self.assertIsNone(installer.backup_root)

    def test_codex_effort_without_tomllib_replaces_recognised_key(self):
        home = self.new_home("effort-no-tomllib-replace-home")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_bytes(b'model_reasoning_effort = "high" # keep\r\n# \xff\r\n')
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        installer = module.Installer(home, REPO, False)
        with mock.patch.dict(sys.modules, {"tomllib": None}), contextlib.redirect_stdout(io.StringIO()) as output:
            module.install_codex_effort(installer, {"codex_effort": "medium"})
        self.assertEqual(config.read_bytes(), b'model_reasoning_effort = "medium" # keep\r\n# \xff\r\n')
        self.assertIn(f"ACTION  {config}  (written)", output.getvalue())

    def test_effort_helpers_apply_defaults_without_install(self):
        home = self.new_home("effort-direct-default-home")
        config = home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_bytes(b'model_reasoning_effort = "medium"\n')
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        installer = module.Installer(home, REPO, False)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            module.install_codex_effort(installer, {})
        self.assertEqual(config.read_bytes(), b'model_reasoning_effort = "high"\n')
        self.assertIn(f"ACTION  {config}  (written)", output.getvalue())
        settings = module.merge_settings({}, {}, home)
        self.assertEqual(settings["modelSettings"], {
            "claude-fable-5-1": {"effortLevel": "medium"},
            "claude-opus-5-5": {"effortLevel": "high"},
            "claude-sonnet-5-5": {"effortLevel": "medium"},
            "claude-haiku-5-5": {"effortLevel": "medium"},
        })

    def test_effort_extended_values_allow_claude_xhigh(self):
        copied = self.copy_codex_effort_repo("effort-extended-repo")
        for effort in ("minimal", "xhigh"):
            for key in ("codex_effort", "claude_effort"):
                with self.subTest(effort=effort, key=key):
                    home = self.new_home(f"effort-extended-{effort}-{key}-home")
                    local = home / ".claude" / "local" / "machine.local.json"
                    local.parent.mkdir(parents=True)
                    local.write_text(json.dumps({key: effort if key == "codex_effort" else {"model": effort}}), encoding="utf-8")
                    config = home / ".codex" / "config.toml"
                    config.parent.mkdir()
                    original = b'model_reasoning_effort = "medium"\n'
                    config.write_bytes(original)
                    result = self.run_install(home, copied)
                    if key == "codex_effort":
                        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                        self.assertEqual(config.read_bytes(), f'model_reasoning_effort = "{effort}"\n'.encode("ascii"))
                    elif effort == "xhigh":
                        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                        settings = read_json_test(home / ".claude" / "settings.json")
                        self.assertEqual(settings["modelSettings"]["model"]["effortLevel"], "xhigh")
                    else:
                        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                        self.assertIn("claude_effort must be a model map with values low, medium, high or xhigh, or null", result.stdout)
                        self.assertNotIn("ACTION ", result.stdout)
                        self.assertEqual(config.read_bytes(), original)

    def test_statusline_weather_writes_fresh_config(self):
        home = self.new_home("weather-fresh-home")
        copied = self.copy_repo_without_local_machine_files("weather-fresh-repo")
        path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(path)
        machine["statusline_weather"] = {"city": "Testville", "lat": 1.5, "lon": 2.5}
        path.write_text(json.dumps(machine), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        config = home / ".claude" / "local" / "statusline.conf"
        self.assertEqual(config.read_bytes(), b"STATUSLINE_CITY=Testville\nSTATUSLINE_LAT=1.5\nSTATUSLINE_LON=2.5\n")
        self.assertIn(f"ACTION  {config}  (written)", result.stdout)

    def test_statusline_weather_preserves_existing_config(self):
        home = self.new_home("weather-existing-home")
        copied = self.copy_repo_without_local_machine_files("weather-existing-repo")
        path = copied / "machines" / f"{TEST_MACHINE}.local.json"
        path.write_text(json.dumps({"statusline_weather": {"city": "Testville", "lat": 1.5, "lon": 2.5}}), encoding="utf-8")
        config = home / ".claude" / "local" / "statusline.conf"
        config.parent.mkdir(parents=True)
        original = b"custom\r\n\xff"
        config.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), original)
        self.assertIn(f"ACTION  {config}  (unchanged)", result.stdout)
        self.assertFalse(list(config.parent.glob("backup-*/.claude/local/statusline.conf")))

    def test_statusline_weather_dry_run_and_home_override(self):
        home = self.new_home("weather-override-home")
        copied = self.copy_repo_without_local_machine_files("weather-override-repo")
        path = copied / "machines" / f"{TEST_MACHINE}.local.json"
        path.write_text(json.dumps({"statusline_weather": None}), encoding="utf-8")
        local = home / ".claude" / "local" / "machine.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"statusline_weather": {"city": "Testville", "lat": 1.5, "lon": 2.5}}), encoding="utf-8")
        config = local.parent / "statusline.conf"
        dry_run = self.run_install(home, copied, ["--dry-run"])
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr or dry_run.stdout)
        self.assertIn(f"ACTION  {config}  (would write)", dry_run.stdout)
        self.assertFalse(config.exists())
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(config.read_bytes(), b"STATUSLINE_CITY=Testville\nSTATUSLINE_LAT=1.5\nSTATUSLINE_LON=2.5\n")

    def test_statusline_weather_inactive_is_silent(self):
        for index, values in enumerate(({}, {"statusline_weather": None}, {"statusline": False, "statusline_weather": {"city": "Testville", "lat": 1.5, "lon": 2.5}})):
            with self.subTest(values=values):
                home = self.new_home(f"weather-inactive-{index}-home")
                copied = self.copy_repo_without_local_machine_files(f"weather-inactive-{index}-repo")
                path = copied / "machines" / f"{TEST_MACHINE}.json"
                machine = read_json_test(path)
                machine.pop("statusline_weather", None)
                machine.update(values)
                path.write_text(json.dumps(machine), encoding="utf-8")
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertFalse((home / ".claude" / "local" / "statusline.conf").exists())
                self.assertNotIn("statusline.conf", result.stdout)

    def test_statusline_weather_home_object_replaces_tracked_object(self):
        home = self.new_home("weather-replace-home")
        copied = self.copy_repo_without_local_machine_files("weather-replace-repo")
        tracked = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(tracked)
        machine["statusline_weather"] = {"city": "Testville source", "lat": 1.5, "lon": 2.5}
        tracked.write_text(json.dumps(machine), encoding="utf-8")
        local = home / ".claude" / "local" / "machine.local.json"
        local.parent.mkdir(parents=True)
        weather = {"city": "Testville", "lat": 1.5, "lon": 2.5}
        local.write_text(json.dumps({"statusline_weather": weather}), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(read_json_test(local.parent / "machine.json")["statusline_weather"], weather)

    def test_deep_merge_replace_policy_is_opt_in(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        weather = {"city": "Testville", "lat": 1.5, "lon": 2.5}
        base = {"section": {"left": 1}, "statusline_weather": weather}
        override = {"section": {"right": 2}, "statusline_weather": {"city": "Testville"}}
        self.assertEqual(installer.deep_merge(base, override), {"section": {"left": 1, "right": 2}, "statusline_weather": weather})
        self.assertEqual(installer.deep_merge(base, override, replace_whole=frozenset({"section"})), {"section": {"right": 2}, "statusline_weather": weather})

    def test_deep_merge_replace_policy_does_not_apply_to_nested_weather(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        weather = {"city": "Testville", "lat": 1.5, "lon": 2.5}
        base = {"statusline_weather": weather, "settings": {"statusline_weather": weather}}
        override = {"statusline_weather": {"city": "Testville"}, "settings": {"statusline_weather": {"lat": 2.5}}}
        result = installer.deep_merge(base, override, replace_whole=frozenset({"statusline_weather"}))
        self.assertEqual(result["statusline_weather"], {"city": "Testville"})
        self.assertEqual(result["settings"]["statusline_weather"], {"city": "Testville", "lat": 2.5, "lon": 2.5})

    def test_statusline_weather_invalid_refuses_install_before_writes(self):
        for present in (False, True):
            for location in ("tracked", "folder", "home"):
                for index, value in enumerate(({"city": "Testville"}, "Testville")):
                    with self.subTest(present=present, location=location, value=value):
                        home = self.new_home(f"weather-invalid-{present}-{location}-{index}-home")
                        copied = self.copy_repo_without_local_machine_files(f"weather-invalid-{present}-{location}-{index}-repo")
                        path = copied / "machines" / f"{TEST_MACHINE}.json"
                        machine = read_json_test(path)
                        machine.pop("statusline_weather", None)
                        if present:
                            machine["statusline_weather"] = {"city": "Testville", "lat": 1.5, "lon": 2.5}
                        path.write_text(json.dumps(machine), encoding="utf-8")
                        if location == "folder":
                            path = path.with_name(f"{TEST_MACHINE}.local.json")
                        elif location == "home":
                            path = home / ".claude" / "local" / "machine.local.json"
                        machine = read_json_test(path) if path.is_file() else {}
                        machine["statusline_weather"] = value
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(machine), encoding="utf-8")
                        before = {item.relative_to(home): item.read_bytes() for item in home.rglob("*") if item.is_file()}
                        result = self.run_install(home, copied)
                        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                        self.assertEqual(result.stdout, f"install refused: {path}: statusline_weather must be null or an object with city, lat and lon\n")
                        self.assertEqual({item.relative_to(home): item.read_bytes() for item in home.rglob("*") if item.is_file()}, before)
                        self.assertNotIn("ACTION ", result.stdout)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_statusline_weather_invalid_refuses_bundle(self):
        for location in ("tracked", "folder"):
            for index, value in enumerate(({"city": "Testville"}, "Testville")):
                with self.subTest(location=location, value=value):
                    copied = self.copy_repo_without_local_machine_files(f"weather-invalid-bundle-{location}-{index}-repo")
                    relative = f"machines/{TEST_MACHINE}{'.local' if location == 'folder' else ''}.json"
                    path = copied / relative
                    machine = read_json_test(path) if path.is_file() else {}
                    machine["statusline_weather"] = value
                    path.write_text(json.dumps(machine), encoding="utf-8")
                    bundle = self.base / f"weather-invalid-{location}-{index}.zip"
                    result = self.run_bundle(copied, bundle)
                    self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                    self.assertEqual(result.stdout.splitlines()[-1], f"bundle refused: {relative}: statusline_weather must be null or an object with city, lat and lon")
                    self.assertFalse(bundle.exists())

    def test_statusline_weather_schema_and_number_rendering(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        valid = {"city": "Testville", "lat": 1.5, "lon": 2.5}
        invalid = [False, [], {}, dict(valid, extra=True), dict(valid, city=""), dict(valid, city="  "), dict(valid, city=1.5), dict(valid, lat=True), dict(valid, lon=False), dict(valid, lat="1.5"), dict(valid, lon=None), dict(valid, lat=91), dict(valid, lat=-91), dict(valid, lon=181), dict(valid, lon=-181), dict(valid, lat=float("nan"))]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "source.json: statusline_weather must be null or an object with city, lat and lon"):
                    installer.validate_statusline_weather({"statusline_weather": value}, lambda key: "source.json")
        for latitude, longitude in ((1.5, 2.5), (-90, -180), (90, 180)):
            with self.subTest(latitude=latitude, longitude=longitude):
                weather = dict(valid, lat=latitude, lon=longitude)
                installer.validate_statusline_weather({"statusline_weather": weather}, lambda key: "source.json")
                writer = mock.Mock()
                installer.install_statusline(writer, REPO, self.base, {"statusline_weather": weather})
                writer.write_bytes.assert_called_once_with(self.base / ".claude" / "local" / "statusline.conf", f"STATUSLINE_CITY=Testville\nSTATUSLINE_LAT={json.dumps(latitude)}\nSTATUSLINE_LON={json.dumps(longitude)}\n".encode("utf-8"))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_statusline_invalid_flag_refuses_bundle(self):
        copied = self.copy_repo_without_local_machine_files("statusline-invalid-bundle-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["statusline"] = "yes"
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "statusline-invalid-bundle.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], f"bundle refused: machines/{TEST_MACHINE}.json: statusline must be true or false")
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_statusline_bundle_contains_script_even_when_disabled(self):
        copied = self.copy_repo_without_local_machine_files("statusline-bundle-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["statusline"] = False
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "statusline-bundle.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(archive.read("claude/statusline-command.sh"), (copied / "claude" / "statusline-command.sh").read_bytes())

    def test_statusline_missing_source_is_skipped(self):
        for present in (False, True):
            with self.subTest(present=present):
                home = self.new_home(f"statusline-missing-{present}-home")
                copied = self.copy_repo_without_local_machine_files(f"statusline-missing-{present}-repo")
                (copied / "claude" / "statusline-command.sh").unlink()
                settings_path = home / ".claude" / "settings.json"
                settings_path.parent.mkdir(parents=True)
                foreign = {"type": "command", "command": "custom-status"}
                settings_path.write_text(json.dumps({"statusLine": foreign} if present else {}), encoding="utf-8")
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                destination = home / ".claude" / "statusline-command.sh"
                self.assertIn(f"ACTION  {destination}  (skipped (no claude/statusline-command.sh in the install folder))", result.stdout)
                self.assertFalse(destination.exists())
                settings = read_json_test(settings_path)
                self.assertEqual("statusLine" in settings, present)
                if present:
                    self.assertEqual(settings["statusLine"], foreign)

    def test_statusline_home_override_preserves_local_config(self):
        home = self.new_home("statusline-home-override-home")
        copied = self.copy_repo_without_local_machine_files("statusline-home-override-repo")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.parent.mkdir(parents=True)
        local_path.write_text(json.dumps({"statusline": False}), encoding="utf-8")
        config = local_path.parent / "statusline.conf"
        content = b"STATUSLINE_CITY=Testville\nSTATUSLINE_LAT=1.5\nSTATUSLINE_LON=2.5\n"
        config.write_bytes(content)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertFalse((home / ".claude" / "statusline-command.sh").exists())
        self.assertNotIn("statusLine", read_json_test(home / ".claude" / "settings.json"))
        self.assertEqual(config.read_bytes(), content)

    def test_statusline_replacement_keeps_script_backup(self):
        home = self.new_home("statusline-backup-home")
        copied = self.copy_repo_without_local_machine_files("statusline-backup-repo")
        destination = home / ".claude" / "statusline-command.sh"
        destination.parent.mkdir(parents=True)
        original = b"#!/usr/bin/env bash\nprintf custom\n"
        destination.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(destination.read_bytes(), (copied / "claude" / "statusline-command.sh").read_bytes())
        backups = list((home / ".claude" / "local").glob("backup-*/.claude/statusline-command.sh"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_keybindings_merge_and_idempotence(self):
        home = self.new_home("keybindings-home")
        copied = self.copy_repo_without_local_machine_files("keybindings-repo")
        path = home / ".claude" / "keybindings.json"
        path.parent.mkdir(parents=True)
        original = {"bindings": [
            {"context": "Chat", "bindings": {"ctrl+e": "chat:externalEditor"}, "custom": True},
            {"context": "Other", "bindings": {"escape": None}},
        ], "custom": "preserved"}
        path.write_text(json.dumps(original), encoding="utf-8")
        first = self.run_install(home, copied)
        self.assertEqual(first.returncode, 0, first.stderr)
        merged = read_json_test(path)
        self.assertEqual(merged["bindings"][0]["bindings"], {
            "ctrl+e": "chat:externalEditor", "meta+v": "chat:imagePaste",
        })
        self.assertTrue(merged["bindings"][0]["custom"])
        self.assertEqual(merged["bindings"][1], original["bindings"][1])
        self.assertEqual(merged["custom"], "preserved")
        before = path.read_bytes()
        before_tree = sorted(item.relative_to(home).as_posix() for item in home.rglob("*"))
        second = self.run_install(home, copied)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(item.relative_to(home).as_posix() for item in home.rglob("*")), before_tree)
        self.assertIn(f"{path}  (unchanged)", second.stdout)

    def test_keybindings_null_is_preserved(self):
        home = self.new_home("keybindings-null-home")
        copied = self.copy_repo_without_local_machine_files("keybindings-null-repo")
        path = home / ".claude" / "keybindings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"bindings": [
            {"context": "Chat", "bindings": {"meta+v": None}},
        ]}), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(read_json_test(path)["bindings"][0]["bindings"]["meta+v"])

    def test_keybindings_invalid_json_uses_defaults(self):
        home = self.new_home("keybindings-invalid-home")
        copied = self.copy_repo_without_local_machine_files("keybindings-invalid-repo")
        path = home / ".claude" / "keybindings.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_json_test(path), read_json_test(copied / "claude" / "keybindings.json"))
        self.assertIn("replaced (existing file was not valid JSON, backup kept)", result.stdout)
        backups = list((home / ".claude" / "local").rglob("keybindings.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "not json")

    def test_bad_keybinding_defaults_are_skipped(self):
        for index, content in enumerate((b"not json", b"[]", b"null", b"\xff")):
            with self.subTest(content=content):
                home = self.new_home(f"bad-default-home-{index}")
                copied = self.copy_repo_without_local_machine_files(f"bad-default-repo-{index}")
                (copied / "claude" / "keybindings.json").write_bytes(content)
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("skipped (claude/keybindings.json in the install folder is not a JSON object)", result.stdout)
                self.assertFalse((home / ".claude" / "keybindings.json").exists())

    def test_keybindings_read_errors_are_handled(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        home = self.new_home("read-error-home")
        path = home / ".claude" / "keybindings.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        installer = mock.Mock()
        defaults = read_json_test(REPO / "claude" / "keybindings.json")
        with mock.patch.object(module, "read_json", side_effect=OSError("unreadable defaults")):
            module.install_keybindings(installer, REPO, home)
        installer.write_bytes.assert_not_called()
        installer.action.assert_called_once_with(path, "skipped (claude/keybindings.json in the install folder is not a JSON object)")
        installer.reset_mock()
        with mock.patch.object(module, "read_json", side_effect=[defaults, OSError("unreadable existing")]):
            module.install_keybindings(installer, REPO, home)
        installer.action.assert_called_once_with(path, "replaced (existing file was not valid JSON, backup kept)")
        installer.write_bytes.assert_called_once_with(path, module.json_bytes(defaults))

    def test_non_object_keybinding_entries_are_preserved(self):
        home = self.new_home("non-object-binding-home")
        copied = self.copy_repo_without_local_machine_files("non-object-binding-repo")
        path = home / ".claude" / "keybindings.json"
        path.parent.mkdir(parents=True)
        original = b'{"bindings": [null, "custom"]}'
        path.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("skipped (unexpected shape, left as is)", result.stdout)

    def test_duplicate_keybinding_contexts_are_preserved(self):
        home = self.new_home("duplicate-binding-home")
        copied = self.copy_repo_without_local_machine_files("duplicate-binding-repo")
        path = home / ".claude" / "keybindings.json"
        path.parent.mkdir(parents=True)
        original = b'{"bindings": [{"context": "Chat"}, {"context": "Chat"}]}'
        path.write_bytes(original)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("skipped (unexpected shape, left as is)", result.stdout)

    def test_keybindings_missing_context_is_appended(self):
        home = self.new_home("keybindings-context-home")
        copied = self.copy_repo_without_local_machine_files("keybindings-context-repo")
        path = home / ".claude" / "keybindings.json"
        path.parent.mkdir(parents=True)
        block = {"context": "Other", "bindings": {"escape": None}}
        path.write_text(json.dumps({"bindings": [block]}), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_json_test(path)["bindings"], [
            block, {"context": "Chat", "bindings": {"meta+v": "chat:imagePaste"}},
        ])

    def test_missing_keybindings_source_is_skipped(self):
        home = self.new_home("keybindings-missing-home")
        copied = self.copy_repo_without_local_machine_files("keybindings-missing-repo")
        (copied / "claude" / "keybindings.json").unlink()
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped (no claude/keybindings.json in the install folder)", result.stdout)
        self.assertFalse((home / ".claude" / "keybindings.json").exists())
        self.assertTrue((home / ".claude" / "settings.json").is_file())

    def test_unexpected_keybindings_shapes_are_preserved(self):
        for index, document in enumerate((
            [], {"bindings": {}}, {"bindings": None},
            {"bindings": [{"context": "Chat", "bindings": []}]},
            {"bindings": [{"context": "Chat", "bindings": None}]},
        )):
            with self.subTest(document=document):
                home = self.new_home(f"keybindings-shape-home-{index}")
                copied = self.copy_repo_without_local_machine_files(f"keybindings-shape-repo-{index}")
                path = home / ".claude" / "keybindings.json"
                path.parent.mkdir(parents=True)
                original = json.dumps(document, indent=4).encode("utf-8")
                path.write_bytes(original)
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(path.read_bytes(), original)
                self.assertIn("skipped (unexpected shape, left as is)", result.stdout)

    def test_keybindings_merge_does_not_alias_defaults(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        defaults = read_json_test(REPO / "claude" / "keybindings.json")
        merged = installer.merge_keybindings({}, defaults)
        self.assertIsNot(merged["bindings"], defaults["bindings"])
        merged["bindings"][0]["bindings"]["meta+v"] = None
        self.assertEqual(defaults["bindings"][0]["bindings"]["meta+v"], "chat:imagePaste")

    def test_draft_wrap_hook_is_installed_once(self):
        home = self.new_home("draft-wrap-home")
        copied = self.copy_repo_without_local_machine_files("draft-wrap-repo")
        for attempt in range(2):
            result = self.run_install(home, copied)
            self.assertEqual(result.returncode, 0, result.stderr)
        settings = read_json_test(home / ".claude" / "settings.json")
        entries = [
            entry for entry in settings["hooks"]["PreToolUse"]
            if entry.get("matcher") == "Edit|MultiEdit|Write|NotebookEdit"
        ]
        self.assertEqual(len(entries), 1)
        commands = [hook["command"] for hook in entries[0]["hooks"]]
        self.assertEqual(sum("draft_wrap_guard.py" in command for command in commands), 1)
        self.assertIn("secret_guard.py", commands[-2])
        self.assertIn("draft_wrap_guard.py", commands[-1])
        machine = read_json_test(copied / "machines" / f"{TEST_MACHINE}.json")
        self.assertEqual(len(commands), 3 if machine.get("codex_first", True) else 2)

    def test_codex_first_hook_merge_respects_flag_and_removes_old_entries(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        home = self.new_home("codex-first-merge-home")
        for machine in ({}, {"codex_first": True}, {"codex_first": False}):
            with self.subTest(machine=machine):
                stale = [{"hooks": [{"command": name}]} for name in (
                    "codex-first-guard.ps1", "codex_first_guard.py", "secret-guard.ps1",
                    "secret_guard.py", "draft_wrap_guard.py",
                )]
                unrelated = {"matcher": "Read", "hooks": [{"command": "custom-pre"}]}
                settings = {"hooks": {"PreToolUse": stale + [unrelated]}}
                filenames = ["secret_guard.py", "draft_wrap_guard.py"]
                if machine.get("codex_first", True):
                    filenames.insert(0, "codex_first_guard.py")
                expected = [installer.command_hook(Path(sys.executable), home / ".claude" / "hooks" / name) for name in filenames]
                for attempt in range(2):
                    settings = installer.merge_settings(settings, machine, home)
                    entries = settings["hooks"]["PreToolUse"]
                    self.assertEqual(len(entries), 3)
                    self.assertEqual(entries[0], unrelated)
                    self.assertEqual(entries[1], {"matcher": "Edit|MultiEdit|Write|NotebookEdit", "hooks": expected})
                    if machine.get("codex_first") is False:
                        self.assertNotIn("codex_first_guard", json.dumps(settings))

    def test_install_home_local_codex_first_overrides_folder_and_keeps_hook_file(self):
        home = self.new_home("codex-first-home")
        copied = self.copy_repo_without_local_machine_files("codex-first-repo")
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"codex_first": True}), encoding="utf-8")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.parent.mkdir(parents=True)
        local_path.write_text(json.dumps({"codex_first": False}), encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertNotIn("codex_first_guard", (home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue((home / ".claude" / "hooks" / "codex_first_guard.py").is_file())

    def test_install_home_publish_controls_clone_markers(self):
        for tracked in (None, False, True):
            for publish in (True, False):
                with self.subTest(tracked=tracked, publish=publish):
                    label = f"home-publish-{tracked}-{publish}"
                    home = self.new_home(label + "-home")
                    copied = self.copy_repo_without_local_machine_files(label + "-repo")
                    machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                    machine = read_json_test(machine_path)
                    machine.pop("publish", None)
                    if tracked is not None:
                        machine["publish"] = tracked
                    machine_path.write_text(json.dumps(machine), encoding="utf-8")
                    (copied / "INSTALL.md").write_text("bundle fixture\n", encoding="utf-8")
                    (copied / ".git").mkdir()
                    (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"publish": not publish}), encoding="utf-8")
                    local_path = home / ".claude" / "local" / "machine.local.json"
                    local_path.parent.mkdir(parents=True)
                    local_path.write_text(json.dumps({"publish": publish}), encoding="utf-8")
                    before = sorted(home.rglob("*"))
                    result = self.run_install(home, copied)
                    if tracked:
                        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                        self.assertEqual((local_path.parent / "harness-mode").read_text(encoding="utf-8"), "bundle\n")
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr or result.stdout)
                        self.assertIn("this install folder holds both INSTALL.md and .git:", result.stderr)
                        self.assertEqual(sorted(home.rglob("*")), before)

    def test_install_rejects_invalid_tracked_publish_despite_local_override(self):
        home = self.new_home("tracked-publish-invalid-home")
        copied = self.copy_repo_without_local_machine_files("tracked-publish-invalid-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["publish"] = "true"
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.parent.mkdir(parents=True)
        local_path.write_text(json.dumps({"publish": True}), encoding="utf-8")
        before = sorted(home.rglob("*"))
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertEqual(result.stdout, f"install refused: {machine_path}: publish must be true or false\n")
        self.assertNotIn("usage:", result.stderr)
        self.assertEqual(sorted(home.rglob("*")), before)

    def test_install_non_boolean_flags_refuse_before_writes_with_source(self):
        for key in ("codex_first", "publish"):
            for location in ("tracked", "folder", "home"):
                for index, value in enumerate(("false", 0, None)):
                    with self.subTest(key=key, location=location, value=value):
                        label = f"invalid-{key}-{location}-{index}"
                        home = self.new_home(label + "-home")
                        copied = self.copy_repo_without_local_machine_files(label + "-repo")
                        path = copied / "machines" / f"{TEST_MACHINE}.json"
                        if location == "folder":
                            path = path.with_name(f"{TEST_MACHINE}.local.json")
                        elif location == "home":
                            path = home / ".claude" / "local" / "machine.local.json"
                        machine = read_json_test(path) if path.is_file() else {}
                        machine[key] = value
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(machine), encoding="utf-8")
                        before = sorted(home.rglob("*"))
                        result = self.run_install(home, copied)
                        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                        self.assertIn(f"install refused: {path}: {key} must be true or false\n", result.stdout)
                        self.assertNotIn("usage:", result.stdout + result.stderr)
                        self.assertFalse((home / ".claude" / "local" / "machine-name").exists())
                        self.assertEqual(sorted(home.rglob("*")), before)

    def test_context_save_hook_merge_preserves_entries_and_is_idempotent(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        home = self.new_home("context-save-merge-home")
        unrelated = {"matcher": "Read", "hooks": [{"type": "command", "command": "custom-post"}]}
        stale = {"matcher": "Write", "hooks": [{"type": "command", "command": "python old/context_save_nudge.py"}]}
        existing = {"hooks": {"PostToolUse": [unrelated, stale, stale]}}
        expected = {"matcher": "Bash|PowerShell|Edit|MultiEdit|Write|Agent", "hooks": [installer.command_hook(
            Path(sys.executable), home / ".claude" / "hooks" / "context_save_nudge.py",
        )]}
        for attempt in range(2):
            existing = installer.merge_settings(existing, {}, home)
            self.assertEqual(existing["hooks"]["PostToolUse"], [unrelated, expected])
        fresh = installer.merge_settings({}, {}, home)
        self.assertEqual(fresh["hooks"]["PostToolUse"], [expected])

    def test_disabled_context_save_hook_removes_stale_entries(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        home = self.new_home("disabled-context-save-home")
        machine = {"name": TEST_MACHINE, "client": False, "os": "linux", "context_save_at": 0, "settings": {}}
        unrelated = {"hooks": [{"type": "command", "command": "custom-post"}]}
        stale = {"hooks": [{"type": "command", "command": "python context_save_nudge.py"}]}
        existing = {"hooks": {"PostToolUse": [stale, unrelated, stale]}}
        for attempt in range(2):
            existing = installer.merge_settings(existing, machine, home)
            self.assertEqual(existing["hooks"]["PostToolUse"], [unrelated])
        self.assertEqual(installer.merge_settings({}, machine, home)["hooks"]["PostToolUse"], [])


    def test_unexpected_post_tool_use_shape_is_replaced(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        home = self.new_home("post-tool-use-shape-home")
        for value in (None, "custom-post", {"hooks": []}, 5):
            for machine in ({}, {"context_save_at": 0}, {"context_save_at": False}):
                with self.subTest(value=value, machine=machine):
                    existing = {"hooks": {"PostToolUse": value}}
                    merged = installer.merge_settings(existing, machine, home)
                    expected = installer.merge_settings({}, machine, home)
                    self.assertEqual(merged["hooks"]["PostToolUse"], expected["hooks"]["PostToolUse"])

    def test_dry_run_writes_nothing(self):
        home = self.new_home("dry-home")
        copied = self.copy_repo_without_local_machine_files("dry-repo")
        result = self.run_install(home, copied, extra=["--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(home.iterdir()), [])
        self.assertIn("(would write)", result.stdout)

    def test_local_machine_override(self):
        home = self.new_home("override-home")
        copied = self.copy_repo_without_local_machine_files("override-repo")
        override = {
            "settings": {"autoMode_environment": ["local override"]}
        }
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(
            json.dumps(override), encoding="utf-8"
        )
        local_facts = "# Local machine facts\n\nLocal test content.\n"
        (copied / "machines" / f"{TEST_MACHINE}.local.md").write_text(
            local_facts, encoding="utf-8"
        )
        result = self.run_install(home, repo=copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = read_json_test(home / ".claude" / "settings.json")
        self.assertEqual(settings["autoMode"]["environment"], ["local override"])
        self.assertEqual(
            (home / ".claude" / "local" / "machine.local.md").read_text(
                encoding="utf-8"
            ),
            local_facts,
        )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_directory(self):
        copied = self.copy_repo_without_local_machine_files("bundle-directory-repo")
        bundle = self.base / "bundle-directory"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        for relative in (
            "install.py",
            "install-manifest.json",
            "INSTALL.md",
            "claude/CLAUDE.md",
            "claude/keybindings.json",
            "claude/hooks/draft_wrap_guard.py",
            "claude/hooks/hooks_test.py",
            f"machines/{TEST_MACHINE}.md",
            f"machines/{TEST_MACHINE}.json",
            "codex/AGENTS.md",
            "checkers/check_writing.py",
            "tools/pr_review_runner.py",
            "tools/pr_review_prompts/review.txt",
        ):
            self.assertTrue((bundle / Path(relative)).is_file(), relative)
        for relative in (
            "bundle-terms.txt",
            "templates/humanizer-install.md",
            "portable-memory",
        ):
            self.assertFalse((bundle / Path(relative)).exists(), relative)
        machine = read_json_test(copied / "machines" / f"{TEST_MACHINE}.json")
        self.assertEqual((bundle / "harvest.py").is_file(), machine.get("harvest", True))
        self.assertEqual((bundle / "README.md").is_file(), machine.get("publish", False))
        other_machines = sorted(
            path.stem for path in (copied / "machines").glob("*.json")
            if not path.name.endswith(".local.json") and path.stem != TEST_MACHINE
        )
        for name in other_machines:
            self.assertFalse((bundle / "machines" / f"{name}.json").exists(), name)

        home = self.new_home("bundle-install-home")
        installed = subprocess.run(
            [
                sys.executable,
                str(bundle / "install.py"),
                "--machine",
                TEST_MACHINE,
                "--home",
                str(home),
                "--no-tests",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertTrue((home / ".claude" / "CLAUDE.md").is_file())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_hook_tests_pass(self):
        if os.environ.get("HARNESS_BUNDLE_TEST_CHILD"):
            return
        copied = self.copy_repo_without_local_machine_files("bundle-hook-tests-repo")
        bundle = self.base / "bundle-hook-tests"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        home = self.new_home("bundle-hook-tests-home")
        environment = os.environ.copy()
        environment["HARNESS_REPO"] = str(bundle)
        environment["HARNESS_BUNDLE_TEST_CHILD"] = "1"
        environment["HOME"] = str(home)
        completed = subprocess.run(
            [sys.executable, str(bundle / "claude" / "hooks" / "hooks_test.py")],
            cwd=bundle,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        test_count = unittest.defaultTestLoader.loadTestsFromModule(
            sys.modules[__name__]
        ).countTestCases()
        self.assertIn(f"Ran {test_count} tests", output)
        self.assertIn("OK", output)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_and_git_markers_are_refused(self):
        copied = self.copy_repo_without_local_machine_files("marker-conflict-repo")
        bundle = self.base / "marker-conflict-bundle"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        (bundle / ".git").mkdir()
        home = self.new_home("marker-conflict-home")
        installed = subprocess.run(
            [
                sys.executable,
                str(bundle / "install.py"),
                "--machine",
                TEST_MACHINE,
                "--home",
                str(home),
                "--no-tests",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(installed.returncode, 1)
        self.assertIn(
            "this install folder holds both INSTALL.md and .git: a bundle was "
            "unpacked over a clone. Delete the folder and unpack the bundle again.",
            installed.stderr,
        )
        self.assertEqual(list(home.iterdir()), [])

    def test_repository_mode(self):
        home = self.new_home("repository-mode-home")
        copied = self.copy_repo_without_local_machine_files("repository-mode-repo")
        (copied / ".git").mkdir()
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (home / ".claude" / "local" / "harness-mode").read_text(
                encoding="utf-8"
            ),
            "repository\n",
        )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_zip(self):
        copied = self.copy_repo_without_local_machine_files("bundle-zip-repo")
        bundle = self.base / "bundle.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue(bundle.is_file())
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
        self.assertIn("INSTALL.md", names)
        self.assertIn("install.py", names)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_harvest_flag_false_omits_export_tooling(self):
        copied = self.copy_repo_without_local_machine_files("bundle-harvest-false-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["harvest"] = False
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "bundle-harvest-false.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            instructions = archive.read("INSTALL.md").decode("utf-8")
        self.assertNotIn("harvest.py", names)
        self.assertFalse(any(name.startswith("claude/skills/harvest/") for name in names))
        self.assertIn("4. Restart Claude Code and run /setup; it asks for what this machine still needs (who you are, git identities, gh account, Codex model, engagement rules) and writes the local files.\n", instructions)
        self.assertNotIn("harvest", instructions)
        for relative in ("install.py", "claude/CLAUDE.md", "claude/hooks/hooks_test.py"):
            self.assertIn(relative, names)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_harvest_default_keeps_export_tooling(self):
        if not (REPO / "harvest.py").is_file():
            self.skipTest("harvest tooling is absent from this bundle")
        copied = self.copy_repo_without_local_machine_files("bundle-harvest-default-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine.pop("harvest", None)
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "bundle-harvest-default.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            instructions = archive.read("INSTALL.md").decode("utf-8")
        self.assertIn("harvest.py", names)
        self.assertIn("claude/skills/harvest/SKILL.md", names)
        self.assertIn("4. Restart Claude Code and run /setup; it asks for what this machine still needs (who you are, git identities, gh account, Codex model, engagement rules) and writes the local files. Memories travel back with `python harvest.py --export <folder>`.\n", instructions)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_harvest_non_boolean_is_refused(self):
        for index, (value, location) in enumerate((("no", "tracked"), (0, "tracked"), ("no", "local"), ("no", "both"))):
            with self.subTest(value=value, location=location):
                copied = self.copy_repo_without_local_machine_files(f"bundle-harvest-invalid-{index}-repo")
                machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                machine = read_json_test(machine_path)
                if location != "local":
                    machine["harvest"] = value
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                source = f"machines/{TEST_MACHINE}.json"
                if location != "tracked":
                    source = f"machines/{TEST_MACHINE}.local.json"
                    (copied / source).write_text(json.dumps({"harvest": value}), encoding="utf-8")
                bundle = self.base / f"bundle-harvest-invalid-{index}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertEqual(result.stdout.splitlines()[-1], f"bundle refused: {source}: harvest must be true or false")
                self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_plan_tools_false_omits_only_plan_tooling(self):
        omitted_files = (
            "tools/masterplan.py", "tools/masterplan_registry.py", "tools/masterplan_test.py",
            "tools/daily_collect.py", "tools/daily_collect_test.py", "tools/project_init.py",
            "tools/project_init_test.py", "tools/transcripts.py", "tools/transcripts_test.py",
            "templates/project-masterplan.json", "templates/project-plan.md", "templates/project-structure.md",
        )
        omitted_folders = (
            "claude/skills/masterplan/", "claude/skills/progress/", "claude/skills/daily/",
            "claude/skills/project-init/", "claude/skills/harness-audit/", "claude/skills/relay-prompt/",
            "claude/skills/t3-insights/", "audit/",
        )
        for location in ("tracked", "local"):
            with self.subTest(location=location):
                copied = self.copy_repo_without_local_machine_files(f"bundle-plan-false-{location}-repo")
                machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                if location == "local":
                    machine_path = machine_path.with_name(f"{TEST_MACHINE}.local.json")
                machine = read_json_test(machine_path) if machine_path.is_file() else {}
                machine["plan_tools"] = False
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                bundle = self.base / f"bundle-plan-false-{location}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                with zipfile.ZipFile(bundle) as archive:
                    names = archive.namelist()
                for name in omitted_files:
                    self.assertNotIn(name, names)
                self.assertFalse(any(name.startswith(omitted_folders) for name in names))
                for name in (
                    "tools/agy_llm.py", "tools/pr_review_runner.py", "tools/pr_review_score.py",
                    "tools/pr_review_test.py", "tools/clip_image.ps1", "tools/pr_review_prompts/review.txt",
                    "checkers/check_writing.py", "claude/skills/humanizer/SKILL.md",
                ):
                    self.assertIn(name, names)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_plan_tools_exclusion_ignores_case_at_every_add_site(self):
        copied = self.copy_repo_without_local_machine_files("bundle-plan-case-repo")
        source = copied / "tools" / "masterplan.py"
        variant = copied / "tools" / "MASTERPLAN.py"
        if source.exists():
            source.rename(variant)
        variant.write_text("# Fixture\n", encoding="utf-8")
        audit = copied / "audit" / "fixture.py"
        audit.parent.mkdir(exist_ok=True)
        audit.write_text("# Fixture\n", encoding="utf-8")
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"plan_tools": False}), encoding="utf-8")
        bundle = self.base / "bundle-plan-case.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            names = [name.casefold() for name in archive.namelist()]
        self.assertNotIn("tools/masterplan.py", names)
        self.assertFalse(any(name.startswith("audit/") for name in names))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_owner_only_exclusion_at_every_add_site(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for index, (plan_tools, publish) in enumerate(((True, True), (True, False), (False, True), (False, False))):
            with self.subTest(plan_tools=plan_tools, publish=publish):
                copied = self.copy_repo_without_local_machine_files(f"bundle-owner-only-{index}-repo")
                for filename in ("release.py", "release_test.py", "RELEASE.py", "RELEASE_TEST.py"):
                    (copied / "tools" / filename).write_text("# Synthetic owner fixture\n", encoding="utf-8")
                (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"plan_tools": plan_tools, "publish": publish}), encoding="utf-8")
                bundle = self.base / f"bundle-owner-only-{index}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                with zipfile.ZipFile(bundle) as archive:
                    names = [name.casefold() for name in archive.namelist()]
                self.assertNotIn("tools/release.py", names)
                self.assertNotIn("tools/release_test.py", names)
                entries = {}
                add, add_tree = installer.bundle_entry_adders(entries, plan_tools=plan_tools)
                source = copied / "tools" / "release.py"
                for relative in ("tools/release.py", "TOOLS/RELEASE.PY", "Tools/Release_Test.Py"):
                    add(source, relative)
                add_tree(copied / "tools", "TOOLS")
                self.assertFalse(any(name.casefold() in installer.OWNER_ONLY_EXCLUDED for name in entries))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_machine_overlay(self):
        copied = self.copy_repo_without_local_machine_files("bundle-overlay-repo")
        overlay = copied / "publish" / TEST_MACHINE
        overlay.mkdir(exist_ok=True)
        (overlay / "README.md").write_text("Synthetic overlay README\n", encoding="utf-8")
        for name in (TEST_MACHINE, "synthetic-overlay-other"):
            if name != TEST_MACHINE:
                for suffix in (".json", ".md"):
                    shutil.copyfile(copied / "machines" / f"{TEST_MACHINE}{suffix}", copied / "machines" / f"{name}{suffix}")
            machine_path = copied / "machines" / f"{name}.json"
            machine = read_json_test(machine_path)
            machine["publish"] = True
            machine_path.write_text(json.dumps(machine), encoding="utf-8")
            bundle = self.base / f"bundle-overlay-{name}.zip"
            result = subprocess.run([sys.executable, str(REPO / "install.py"), "--bundle", name, "--out", str(bundle), "--repo", str(copied), "--home", str(self.bundle_home(copied))], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            with zipfile.ZipFile(bundle) as archive:
                source = overlay / "README.md" if name == TEST_MACHINE else copied / "publish" / "README.md"
                self.assertEqual(archive.read("README.md"), source.read_bytes())
                self.assertEqual(archive.read("LICENSE"), (copied / "publish" / "LICENSE").read_bytes())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_overlay_refuses_stray_entries(self):
        for index, directory in enumerate((False, True)):
            with self.subTest(directory=directory):
                copied = self.copy_repo_without_local_machine_files(f"bundle-overlay-stray-{index}-repo")
                overlay = copied / "publish" / TEST_MACHINE
                overlay.mkdir(exist_ok=True)
                stray = overlay / ("README.md" if directory else "stray.txt")
                if directory:
                    stray.mkdir()
                else:
                    stray.write_text("Synthetic stray\n", encoding="utf-8")
                (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text('{"publish": true}', encoding="utf-8")
                bundle = self.base / f"bundle-overlay-stray-{index}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn(stray.relative_to(copied).as_posix(), result.stdout)
                self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_overlay_passes_through_gate(self):
        copied = self.copy_repo_without_local_machine_files("bundle-overlay-gate-repo")
        overlay = copied / "publish" / TEST_MACHINE
        overlay.mkdir(exist_ok=True)
        (overlay / "README.md").write_text("bundleprobe" + " exclusion\n", encoding="utf-8")
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text('{"publish": true}', encoding="utf-8")
        bundle = self.base / "bundle-overlay-gate.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("GATE README.md:1:", result.stdout)
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_overlay_keeps_root_files_required(self):
        copied = self.copy_repo_without_local_machine_files("bundle-overlay-required-repo")
        overlay = copied / "publish" / TEST_MACHINE
        overlay.mkdir(exist_ok=True)
        shutil.copyfile(copied / "publish" / "README.md", overlay / "README.md")
        (copied / "publish" / "README.md").unlink()
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text('{"publish": true}', encoding="utf-8")
        result = self.run_bundle(copied, self.base / "bundle-overlay-required.zip")
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("publish/README.md is missing", result.stdout)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_overlay_preserves_root_notices(self):
        for index, preserve in enumerate((False, True)):
            with self.subTest(preserve=preserve):
                copied = self.copy_repo_without_local_machine_files(f"bundle-overlay-notices-{index}-repo")
                overlay = copied / "publish" / TEST_MACHINE
                overlay.mkdir(exist_ok=True)
                root = (copied / "publish" / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
                path = overlay / "THIRD-PARTY-NOTICES.md"
                text = (root if preserve else "") + "\nSynthetic additional notice.\n"
                path.write_text(text, encoding="utf-8")
                (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text('{"publish": true}', encoding="utf-8")
                bundle = self.base / f"bundle-overlay-notices-{index}.zip"
                result = self.run_bundle(copied, bundle)
                if preserve:
                    self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                    with zipfile.ZipFile(bundle) as archive:
                        self.assertEqual(archive.read("THIRD-PARTY-NOTICES.md"), path.read_bytes())
                else:
                    self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                    self.assertIn(path.relative_to(copied).as_posix(), result.stdout)
                    self.assertIn("must contain the full root", result.stdout)
                    self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_overlay_cannot_override_license(self):
        copied = self.copy_repo_without_local_machine_files("bundle-overlay-license-repo")
        overlay = copied / "publish" / TEST_MACHINE
        overlay.mkdir(exist_ok=True)
        path = overlay / "LICENSE"
        shutil.copyfile(copied / "publish" / "LICENSE", path)
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text('{"publish": true}', encoding="utf-8")
        bundle = self.base / "bundle-overlay-license.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn(path.relative_to(copied).as_posix(), result.stdout)
        self.assertIn("LICENSE cannot be overridden", result.stdout)
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_plan_tools_default_keeps_plan_tooling(self):
        if not (REPO / "tools" / "masterplan.py").is_file():
            self.skipTest("plan tooling is absent from this bundle")
        copied = self.copy_repo_without_local_machine_files("bundle-plan-default-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine.pop("plan_tools", None)
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "bundle-plan-default.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            for name in ("tools/masterplan.py", "claude/skills/masterplan/SKILL.md", "audit/hist.py", "templates/project-structure.md"):
                self.assertIn(name, archive.namelist())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_plan_tools_and_publish_non_boolean_are_refused(self):
        for key in ("plan_tools", "publish"):
            for index, (value, location) in enumerate((("no", "tracked"), (0, "tracked"), ("no", "local"), ("no", "both"))):
                with self.subTest(key=key, value=value, location=location):
                    copied = self.copy_repo_without_local_machine_files(f"bundle-{key}-invalid-{index}-repo")
                    machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                    machine = read_json_test(machine_path)
                    if location != "local":
                        machine[key] = value
                    machine_path.write_text(json.dumps(machine), encoding="utf-8")
                    source = f"machines/{TEST_MACHINE}.json"
                    if location != "tracked":
                        source = f"machines/{TEST_MACHINE}.local.json"
                        (copied / source).write_text(json.dumps({key: value}), encoding="utf-8")
                    bundle = self.base / f"bundle-{key}-invalid-{index}.zip"
                    result = self.run_bundle(copied, bundle)
                    self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                    self.assertEqual(result.stdout.splitlines()[-1], f"bundle refused: {source}: {key} must be true or false")
                    self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_entries_and_instructions(self):
        for index, value in enumerate((True, False, None)):
            with self.subTest(publish=value):
                copied = self.copy_repo_without_local_machine_files(f"bundle-publish-{index}-repo")
                machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                machine = read_json_test(machine_path)
                machine.pop("publish", None)
                if value is not None:
                    machine["publish"] = value
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                bundle = self.base / f"bundle-publish-{index}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                with zipfile.ZipFile(bundle) as archive:
                    for name in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md", ".gitignore", ".gitattributes"):
                        if value:
                            self.assertEqual(archive.read(name), (copied / "publish" / name).read_bytes())
                        else:
                            self.assertNotIn(name, archive.namelist())
                    instructions = archive.read("INSTALL.md").decode("utf-8")
                first = (f"1. Clone the repository as the harness install folder named in machines/{TEST_MACHINE}.md, or unpack this folder there. Update it later with git pull; a release replaces the shared files and never touches your local files."
                         if value else f"1. On a first install, unpack the zip as the harness install folder named in machines/{TEST_MACHINE}.md. On every later update, run `python install.py --unpack <zip>` inside the existing install folder (use the interpreter the machine file names), which writes the shipped files, removes files the previous release shipped and this one does not, and keeps everything else (handoffs, `.scratch/`, `bundles/`, `inbox/`, your local machine files, which it backs up under `.scratch/` before replacing). If the install folder's install.py does not know `--unpack` yet, extract install.py from the zip first (`unzip -o <zip> install.py` or Expand-Archive for that one file) and run that one.")
                steps = [line for line in instructions.splitlines() if re.match(r"[1-4]\. ", line)]
                self.assertEqual(steps[0], first)
                self.assertIn("Bundle gate applied:", result.stdout)
                self.assertIn("fingerprints", result.stdout)
                if value:
                    self.assertNotIn("Bundle gate applied", instructions)
                    self.assertNotIn(" from ", instructions.splitlines()[0])
                    self.assertNotIn("fingerprints", instructions)
                    self.assertEqual(steps[3], "4. Restart Claude Code and run /setup; it asks who you are, your git identities and gh account, and whether you use Codex CLI, and writes the local files.")
                else:
                    self.assertIn("Bundle gate applied:", instructions)
                    self.assertIn(" from ", instructions.splitlines()[0])
                    self.assertIn("fingerprints", instructions)
                if index == 0:
                    shared_steps = steps[1:3]
                else:
                    self.assertEqual(steps[1:3], shared_steps)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_public_excludes_usage_skill_but_addon_includes_it(self):
        copied = self.copy_repo_without_local_machine_files("bundle-public-usage-repo")
        bundle = self.base / "bundle-public-usage.zip"
        result = subprocess.run(
            [sys.executable, str(REPO / "install.py"), "--bundle", "public",
             "--out", str(bundle), "--repo", str(copied), "--home", str(self.bundle_home(copied))],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            self.assertFalse(any(name.startswith("claude/skills/usage/") for name in archive.namelist()))
        addon = self.base / "bundle-public-usage-addon.zip"
        result = self.run_addon(copied, addon, name="usage")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(addon) as archive:
            self.assertIn("claude/skills/usage/SKILL.md", archive.namelist())

    def test_bundle_install_text_keeps_audit_details_only_when_not_published(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        date = "2026-09-16"
        commit = "fixture-commit"
        record = "Bundle gate applied: 1 global terms, 1 domain lists with 1 terms, fingerprints 1234abcd (owned: none)"
        for publish in (True, False):
            for harvest in (True, False):
                with self.subTest(publish=publish, harvest=harvest):
                    text = installer.bundle_install_text(TEST_MACHINE, date, commit, record, harvest=harvest, publish=publish)
                    header = f"# Harness bundle for {TEST_MACHINE}, built {date}"
                    if publish:
                        self.assertEqual(text.splitlines()[0], header)
                        for fragment in (commit, record, "Bundle gate applied", "fingerprints", "1234abcd"):
                            self.assertNotIn(fragment, text)
                        self.assertEqual(text.splitlines()[-1], "4. Restart Claude Code and run /setup; it asks who you are, your git identities and gh account, and whether you use Codex CLI, and writes the local files.")
                    else:
                        restart = "4. Restart Claude Code and run /setup; it asks for what this machine still needs (who you are, git identities, gh account, Codex model, engagement rules) and writes the local files."
                        if harvest:
                            restart += " Memories travel back with `python harvest.py --export <folder>`."
                        self.assertEqual(text, (
                            f"{header} from {commit}\n\n{record}\n\n"
                            f"1. On a first install, unpack the zip as the harness install folder named in machines/{TEST_MACHINE}.md. On every later update, run `python install.py --unpack <zip>` inside the existing install folder (use the interpreter the machine file names), which writes the shipped files, removes files the previous release shipped and this one does not, and keeps everything else (handoffs, `.scratch/`, `bundles/`, `inbox/`, your local machine files, which it backs up under `.scratch/` before replacing). If the install folder's install.py does not know `--unpack` yet, extract install.py from the zip first (`unzip -o <zip> install.py` or Expand-Archive for that one file) and run that one.\n"
                            f"2. Dry run and read every line: `python install.py --machine {TEST_MACHINE} --dry-run` (use the interpreter the machine file names; where the permission classifier blocks config edits, run the real install with the ! prefix).\n"
                            f"3. Real run: `python install.py --machine {TEST_MACHINE}`. The hook tests run last and all must pass.\n"
                            f"{restart}\n"
                        ))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_missing_file_is_refused(self):
        for index, name in enumerate(("LICENSE", ".gitattributes")):
            with self.subTest(name=name):
                copied = self.copy_repo_without_local_machine_files(f"bundle-publish-missing-{index}-repo")
                (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"publish": True}), encoding="utf-8")
                (copied / "publish" / name).unlink()
                bundle = self.base / f"bundle-publish-missing-{index}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertEqual(result.stdout.splitlines()[-1], f"bundle refused: publish/{name} is missing")
                self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_local_override_entries_pass_through_gate(self):
        copied = self.copy_repo_without_local_machine_files("bundle-publish-gate-repo")
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"publish": True}), encoding="utf-8")
        for name in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md", ".gitignore", ".gitattributes"):
            with (copied / "publish" / name).open("a", encoding="utf-8") as handle:
                handle.write("\nbundleprobe" + " exclusion\n")
        bundle = self.base / "bundle-publish-gate.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        for name in ("README.md", "LICENSE", "THIRD-PARTY-NOTICES.md", ".gitignore", ".gitattributes"):
            self.assertIn(f"GATE {name}:", result.stdout)
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_codex_first_non_boolean_is_refused(self):
        for index, (value, location) in enumerate((("no", "tracked"), (0, "tracked"), (None, "local"), ("no", "both"))):
            with self.subTest(value=value, location=location):
                copied = self.copy_repo_without_local_machine_files(f"bundle-codex-first-invalid-{index}-repo")
                machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
                machine = read_json_test(machine_path)
                if location != "local":
                    machine["codex_first"] = value
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                source = f"machines/{TEST_MACHINE}.json"
                if location != "tracked":
                    source = f"machines/{TEST_MACHINE}.local.json"
                    (copied / source).write_text(json.dumps({"codex_first": value}), encoding="utf-8")
                bundle = self.base / f"bundle-codex-first-invalid-{index}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertEqual(result.stdout.splitlines()[-1], f"bundle refused: {source}: codex_first must be true or false")
                self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_published_bundle_installs_from_clone_and_hook_tests_pass(self):
        if os.environ.get("HARNESS_BUNDLE_TEST_CHILD"):
            self.skipTest("nested bundle test run")
        copied = self.copy_repo_without_local_machine_files("published-clone-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine.update({"harvest": False, "plan_tools": False, "codex_first": False, "publish": True, "delete": []})
        machine.pop("codex_model", None)
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "published-clone.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        unpacked = self.base / "published-clone"
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(unpacked)
        self.assertTrue((unpacked / ".gitattributes").is_file())
        (unpacked / ".git").mkdir()
        home = self.new_home("published-clone-home")
        installed = subprocess.run(
            [sys.executable, str(unpacked / "install.py"), "--machine", TEST_MACHINE, "--home", str(home), "--no-tests"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
        self.assertNotIn("codex_model", read_json_test(home / ".claude" / "local" / "machine.json"))
        self.assertEqual((home / ".claude" / "local" / "harness-mode").read_text(encoding="utf-8"), "bundle\n")
        self.assertNotIn("codex_first_guard", (home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        for relative in ("tools/masterplan.py", "audit", "harvest.py", "claude/skills/harvest"):
            self.assertFalse((unpacked / relative).exists(), relative)
        self.assertEqual(list((unpacked / "machines").glob("*.json")), [unpacked / "machines" / f"{TEST_MACHINE}.json"])
        environment = os.environ.copy()
        environment.pop("HARNESS_TEST_MACHINE", None)
        environment["HARNESS_REPO"] = str(unpacked)
        environment["HARNESS_BUNDLE_TEST_CHILD"] = "1"
        environment["HOME"] = str(home)
        completed = subprocess.run(
            [sys.executable, str(unpacked / "claude" / "hooks" / "hooks_test.py")],
            cwd=unpacked, env=environment, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600, check=False,
        )
        output = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 0, output)
        test_count = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]).countTestCases()
        self.assertIn(f"Ran {test_count} tests", output)
        self.assertIn("OK", output)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_directory_exclusion_ignores_case_and_trailing_slash(self):
        if not (REPO / "claude" / "skills" / "harvest").is_dir():
            self.skipTest("harvest skill is absent from this bundle")
        copied = self.copy_repo_without_local_machine_files("bundle-exclusion-case-repo")
        skill = copied / "claude" / "skills" / "harvest"
        skill.rename(skill.with_name("HaRvEsT"))
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["harvest"] = False
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "bundle-exclusion-case.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
        self.assertFalse(any(name.casefold().startswith("claude/skills/harvest/") for name in names))
        self.assertNotIn("templates/humanizer-install.md", names)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_machine_terms_refuse(self):
        copied = self.copy_repo_without_local_machine_files("bundle-machine-gate-repo")
        whole_term = "bundleprobe" + " word"
        substring_term = "bundleprobe" + "fragment"
        terms_path = self.bundle_terms_file(copied)
        terms_path.write_text(
            f"# Machine exclusions\n\n{whole_term}\nsubstr:{substring_term}\n",
            encoding="utf-8",
        )
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        original = skill.read_text(encoding="utf-8")
        line_number = len((original + "\n").splitlines()) + 1
        for term, content in (
            (whole_term, whole_term.upper().replace(" ", "\t")),
            (substring_term, "prefix" + substring_term.upper() + "suffix"),
        ):
            with self.subTest(term=term):
                skill.write_text(original + "\n" + content + "\n", encoding="utf-8")
                bundle = self.base / "bundle-machine-refused"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn(
                    f"GATE claude/skills/save/SKILL.md:{line_number}: {term}", result.stdout
                )
                self.assertIn("bundle refused: 1 hits", result.stdout)
                self.assertRegex(result.stdout, r"bundle gate: [1-9]\d* global terms, 2 terms from 1 domain lists, owned: none")
                self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_machine_terms_stay_out(self):
        copied = self.copy_repo_without_local_machine_files("bundle-machine-clean-repo")
        whole_term = "bundleprobe" + " word"
        substring_term = "bundleprobe" + "fragment"
        terms_path = self.bundle_terms_file(copied)
        terms_path.write_text(
            f"# Machine exclusions\n\n{whole_term}\nsubstr:{substring_term}\n",
            encoding="utf-8",
        )
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\nprefix" + whole_term + "suffix\n")
        bundle = self.base / "bundle-machine-clean.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("bundle written", result.stdout)
        self.assertRegex(result.stdout, r"bundle gate: [1-9]\d* global terms, 2 terms from 1 domain lists, owned: none")
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
        self.assertFalse(any(Path(name).name == terms_path.name for name in names))
        self.assertFalse(any("bundle-terms" in Path(name).parts for name in names))
        self.assertFalse(any(name.endswith(".bundle-terms.txt") for name in names))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_requires_domain_lists(self):
        for client in (True, False):
            with self.subTest(client=client):
                copied = self.copy_repo_without_local_machine_files(f"bundle-no-domains-{client}-repo", client=client, terms=False)
                bundle = self.base / f"bundle-no-domains-{client}"
                result = self.run_bundle(copied, bundle)
                folder = self.bundle_terms_file(copied).parent
                self.assert_bundle_refused(
                    result, bundle,
                    f"bundle refused: no domain lists under {folder} (one file per engagement or product family, see README)",
                )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_missing_owned_domain(self):
        copied = self.copy_repo_without_local_machine_files("bundle-missing-owned-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = ["missing"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "bundle-missing-owned"
        result = self.run_bundle(copied, bundle)
        missing = self.bundle_terms_file(copied).with_name("missing.txt")
        self.assert_bundle_refused(result, bundle, f"bundle refused: machines/{TEST_MACHINE}.json owns missing but {missing} does not exist")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_terms_file_entry(self):
        copied = self.copy_repo_without_local_machine_files("bundle-terms-entry-repo")
        relative = "claude/skills/save/probe.bundle-terms.txt"
        (copied / relative).write_text("# Exclusions\n", encoding="utf-8")
        bundle = self.base / "bundle-terms-entry-refused"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(
            result, bundle, f"bundle refused: terms file inside the bundle tree: {relative}", machine_terms=1
        )

    def assert_bundle_refused(self, result, bundle, message, machine_terms=None, owned="none", domain_count=1):
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        if machine_terms is None:
            self.assertNotIn("bundle gate:", result.stdout)
        else:
            self.assertRegex(
                next(line for line in result.stdout.splitlines() if re.match(r"bundle gate: \d", line)), rf"^bundle gate: \d+ global terms, {machine_terms} terms from {domain_count} domain lists, owned: {owned}$"
            )
        self.assertIn(message + "\n", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertFalse(bundle.exists())

    def test_machine_clients_are_boolean(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for name in installer.machine_names(REPO):
            with self.subTest(name=name):
                self.assertIsInstance(read_json_test(REPO / "machines" / f"{name}.json").get("client"), bool)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_invalid_owns(self):
        copied = self.copy_repo_without_local_machine_files("bundle-invalid-owns-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        for value in (1, True, "alpha", None, {}, [""], [" "], [1]):
            with self.subTest(value=value):
                machine["owns"] = value
                machine_path.write_text(json.dumps(machine), encoding="utf-8")
                bundle = self.base / "bundle-invalid-owns"
                result = self.run_bundle(copied, bundle)
                self.assert_bundle_refused(result, bundle, f"bundle refused: machines/{TEST_MACHINE}.json: owns must be a list of non-empty strings")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_owned_domains_are_not_applied(self):
        copied = self.copy_repo_without_local_machine_files("bundle-owned-repo", terms=False)
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = ["alpha"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        folder = self.bundle_terms_file(copied).parent
        alpha_term = "domainprobe" + "alpha"
        beta_term = "domainprobe" + "beta"
        (folder / "alpha.txt").write_text(alpha_term + "\n", encoding="utf-8")
        (folder / "beta.txt").write_text(beta_term + "\n", encoding="utf-8")
        self.declare_bundle_domains(copied, ["beta"])
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        original = skill.read_text(encoding="utf-8")
        skill.write_text(original + "\n" + alpha_term + "\n" + beta_term + "\n", encoding="utf-8")
        bundle = self.base / "bundle-owned"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1, owned="alpha")
        self.assertIn(": " + beta_term + "\n", result.stdout)
        self.assertNotIn(": " + alpha_term + "\n", result.stdout)
        skill.write_text(original + "\n" + alpha_term + "\n", encoding="utf-8")
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue((bundle / "INSTALL.md").is_file())
        record = next(line for line in (bundle / "INSTALL.md").read_text(encoding="utf-8").splitlines() if line.startswith("Bundle gate applied:"))
        self.assertRegex(record, r"^Bundle gate applied: \d+ global terms, 1 domain lists with 1 terms, fingerprints [0-9a-f]{8} \(owned: alpha\)$")
        self.assertNotIn("beta", record)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_when_every_domain_is_owned(self):
        copied = self.copy_repo_without_local_machine_files("bundle-all-owned-repo", terms=False)
        domain = "alpha"
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = [domain]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        (self.bundle_terms_file(copied).parent / f"{domain}.txt").write_text(domain + "\n", encoding="utf-8")
        bundle = self.base / "bundle-all-owned"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(
            result, bundle,
            f"bundle refused: no domain list applies to {TEST_MACHINE} (every list on the builder is owned by it); a client bundle must exclude at least one other domain",
        )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_audit_records_sorted_fingerprints_of_applied_bytes(self):
        copied = self.copy_repo_without_local_machine_files("bundle-fingerprints-repo", terms=False)
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = ["alpha"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        folder = self.bundle_terms_file(copied).parent
        (folder / "alpha.txt").write_bytes(b"alpha\n")
        beta = "fingerprint" + "beta"
        gamma = "fingerprint" + "gamma"
        contents = {beta: ("# First list\r\n" + beta + "\r\n").encode("utf-8-sig"), gamma: ("# Second list\n" + gamma + "\n").encode("utf-8")}
        for domain, content in contents.items():
            (folder / f"{domain}.txt").write_bytes(content)
        self.declare_bundle_domains(copied, list(contents))
        bundle = self.base / "bundle-fingerprints"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        record = next(line for line in (bundle / "INSTALL.md").read_text(encoding="utf-8").splitlines() if line.startswith("Bundle gate applied:"))
        fingerprints = ", ".join(sorted(hashlib.sha256(content).hexdigest()[:8] for content in contents.values()))
        self.assertRegex(record, rf"^Bundle gate applied: \d+ global terms, 2 domain lists with 2 terms, fingerprints {fingerprints} \(owned: alpha\)$")
        for domain in contents:
            self.assertNotIn(domain, record)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_reports_ignored_domain_entries(self):
        copied = self.copy_repo_without_local_machine_files("bundle-ignored-domain-repo")
        ignored = "probe.txt.bak"
        (self.bundle_terms_file(copied).parent / ignored).write_bytes(b"\xff\xfe")
        bundle = self.base / "bundle-ignored-domain"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(result.stdout.count(f"bundle gate: ignoring {ignored} (not a .txt list)\n"), 1)
        self.assertRegex(result.stdout, r"bundle gate: \d+ global terms, 1 terms from 1 domain lists, owned: none")
        self.assertTrue((bundle / "INSTALL.md").is_file())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_missing_domain_owned_elsewhere(self):
        copied = self.copy_repo_without_local_machine_files("bundle-required-domain-repo")
        other = "domain-owner"
        self.declare_bundle_domains(copied, ["probe", "beta"])
        bundle = self.base / "bundle-required-domain"
        result = self.run_bundle(copied, bundle)
        missing = self.bundle_terms_file(copied).with_name("beta.txt")
        self.assert_bundle_refused(result, bundle, f"bundle refused: domain beta is owned by {other} but {missing} does not exist")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_ownership_lookup_ignores_case(self):
        copied = self.copy_repo_without_local_machine_files("bundle-domain-case-repo", terms=False)
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = ["ALPHA"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        (copied / "machines" / "domain-owner.json").write_text(json.dumps({"owns": ["BETA"]}), encoding="utf-8")
        folder = self.bundle_terms_file(copied).parent
        (folder / "Alpha.txt").write_text("domainprobe" + "alpha\n", encoding="utf-8")
        (folder / "beta.txt").write_text("domainprobe" + "beta\n", encoding="utf-8")
        bundle = self.base / "bundle-domain-case"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertRegex(result.stdout, r"bundle gate: \d+ global terms, 1 terms from 1 domain lists, owned: ALPHA")
        self.assertRegex((bundle / "INSTALL.md").read_text(encoding="utf-8"), r"Bundle gate applied: \d+ global terms, 1 domain lists with 1 terms, fingerprints [0-9a-f]{8} \(owned: ALPHA\)")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_untracked_machine_cannot_require_domain(self):
        copied = self.copy_repo_without_local_machine_files("bundle-untracked-owner-repo")
        self.initialize_bundle_git(copied)
        (copied / "machines" / "untracked.json").write_text(json.dumps({"owns": ["missing"]}), encoding="utf-8")
        bundle = self.base / "bundle-untracked-owner"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue((bundle / "INSTALL.md").is_file())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_staged_machine_does_not_change_ownership(self):
        copied = self.copy_repo_without_local_machine_files("bundle-staged-owner-repo")
        self.initialize_bundle_git(copied)
        (copied / "machines" / "staged.json").write_text(json.dumps({"owns": ["missing", "probe"]}), encoding="utf-8")
        result = subprocess.run(["git", "add", "machines/staged.json"], cwd=copied, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = self.base / "bundle-staged-owner"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue((bundle / "INSTALL.md").is_file())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_repository_without_commit_refuses(self):
        copied = self.copy_repo_without_local_machine_files("bundle-no-commit-repo")
        for command in (["git", "init", "-q"], ["git", "add", "machines"]):
            result = subprocess.run(command, cwd=copied, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        bundle = self.base / "bundle-no-commit"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, f"bundle refused: {copied} has no commit to read machine ownership from")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_reads_committed_machine_with_bom(self):
        copied = self.copy_repo_without_local_machine_files("bundle-bom-machine-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = ["alpha"]
        machine_path.write_bytes(json.dumps(machine).encode("utf-8-sig"))
        self.bundle_terms_file(copied).with_name("alpha.txt").write_text("domainprobe" + "alpha\n", encoding="utf-8")
        self.initialize_bundle_git(copied)
        bundle = self.base / "bundle-bom-machine"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertRegex(result.stdout, r"bundle gate: \d+ global terms, 1 terms from 1 domain lists, owned: alpha")
        self.assertTrue((bundle / "INSTALL.md").exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_dirty_machine_settings(self):
        copied = self.copy_repo_without_local_machine_files("bundle-dirty-settings-repo")
        self.initialize_bundle_git(copied)
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["health"] = "uncommitted setting"
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        bundle = self.base / "bundle-dirty-settings"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, f"bundle refused: machines/{TEST_MACHINE}.json has uncommitted changes; commit them so the shipped file matches the ownership the gate applied")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_empty_domain_list(self):
        copied = self.copy_repo_without_local_machine_files("bundle-empty-domain-repo")
        path = self.bundle_terms_file(copied)
        for content in ("", "# Terms\n\n"):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                bundle = self.base / "bundle-empty-domain"
                result = self.run_bundle(copied, bundle)
                self.assert_bundle_refused(result, bundle, f"bundle refused: {path} holds no terms")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_unicode_capital_i_matches_itself(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        value = "\u0130stanbul" + "Bank"
        for prefix in ("", "substr:"):
            with self.subTest(prefix=prefix), contextlib.redirect_stdout(io.StringIO()) as stdout:
                terms = installer.parse_bundle_terms(prefix + value, "test")
                self.assertEqual(installer.bundle_gate([("probe.md", value.encode("utf-8"))], terms), 1)
                self.assertEqual(stdout.getvalue(), f"GATE probe.md:1: {value}\n")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_untracked_target(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-untracked-target-repo")
        self.initialize_bundle_git(copied)
        (copied / "machines" / "leak.json").write_text(json.dumps({"owns": ["probe"]}), encoding="utf-8")
        bundle = self.base / "bundle-untracked-target"
        options = {"bundle": "leak", "repo": copied, "home": self.bundle_home(copied), "out": bundle}
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(installer.build_bundle(options), 2)
        available = ", ".join(installer.tracked_machine_names(copied))
        self.assertEqual(stdout.getvalue(), f"bundle refused: machines/leak.json is not a tracked machine; available machines: {available}\n")
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_uncommitted_owns_cannot_skip_domain(self):
        copied = self.copy_repo_without_local_machine_files("bundle-committed-owns-repo")
        self.initialize_bundle_git(copied)
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = ["probe"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        term = self.bundle_terms_file(copied).read_text(encoding="utf-8").strip()
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + term + "\n")
        bundle = self.base / "bundle-committed-owns"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, f"bundle refused: machines/{TEST_MACHINE}.json has uncommitted changes; commit them so the shipped file matches the ownership the gate applied")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_unowned_domain_list(self):
        copied = self.copy_repo_without_local_machine_files("bundle-unowned-repo")
        self.declare_bundle_domains(copied, [])
        bundle = self.base / "bundle-unowned"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, f"bundle refused: domain list {self.bundle_terms_file(copied)} is owned by no machine; declare it in one machines/<name>.json owns list")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_accepts_domain_owned_by_multiple_machines(self):
        copied = self.copy_repo_without_local_machine_files("bundle-multiple-owners-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["owns"] = ["PROBE"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        shared_term = "domainprobe" + "shared"
        excluded_term = "domainprobe" + "excluded"
        self.bundle_terms_file(copied).write_text(shared_term + "\n", encoding="utf-8")
        self.bundle_terms_file(copied).with_name("beta.txt").write_text(excluded_term + "\n", encoding="utf-8")
        (copied / "machines" / "excluded-owner.json").write_text(json.dumps({"owns": ["beta"]}), encoding="utf-8")
        self.initialize_bundle_git(copied)
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        original = skill.read_text(encoding="utf-8")
        for name, owned in ((TEST_MACHINE, "PROBE"), ("domain-owner", "probe")):
            with self.subTest(name=name), mock.patch.dict(globals(), TEST_MACHINE=name):
                bundle = self.base / f"bundle-multiple-owners-{name}"
                skill.write_text(original + "\n" + shared_term + "\n" + excluded_term + "\n", encoding="utf-8")
                result = self.run_bundle(copied, bundle)
                self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1, owned=owned)
                self.assertIn(": " + excluded_term + "\n", result.stdout)
                self.assertNotIn(": " + shared_term + "\n", result.stdout)
                skill.write_text(original + "\n" + shared_term + "\n", encoding="utf-8")
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertTrue((bundle / "INSTALL.md").is_file())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_missing_shared_domain_names_all_owners(self):
        copied = self.copy_repo_without_local_machine_files("bundle-missing-shared-repo")
        self.declare_bundle_domains(copied, ["probe", "beta"])
        (copied / "machines" / "additional-owner.json").write_text(json.dumps({"owns": ["beta"]}), encoding="utf-8")
        bundle = self.base / "bundle-missing-shared"
        missing = self.bundle_terms_file(copied).with_name("beta.txt")
        message = f"domain beta is owned by additional-owner, domain-owner but {missing} does not exist"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: " + message)
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with self.assertRaises(installer.BundleRefusal) as refusal:
            installer.all_bundle_terms(self.bundle_home(copied), repo=copied)
        self.assertEqual(str(refusal.exception), message)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_case_colliding_domain_files(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-collision-repo")
        path = self.bundle_terms_file(copied)
        collision = path.with_name("Probe.txt")
        original_iterdir = Path.iterdir

        def iterdir(directory):
            return iter([path, collision]) if directory == path.parent else original_iterdir(directory)

        bundle = self.base / "bundle-collision"
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied), "out": bundle}
        with mock.patch.object(Path, "iterdir", iterdir), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(installer.build_bundle(options), 2)
        self.assertIn(str(path), stdout.getvalue())
        self.assertIn(str(collision), stdout.getvalue())
        self.assertIn("have the same case-insensitive name", stdout.getvalue())
        self.assertNotIn("bundle gate:", stdout.getvalue())
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_git_read_failures_are_named_refusals(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-git-failures-repo")
        (copied / ".git").mkdir()
        bundle = self.base / "bundle-git-failures"
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied), "out": bundle}
        for command in ("ls-tree", "cat-file"):
            for failure in (OSError("git unavailable"), subprocess.CompletedProcess([], 1, "", "git failed")):
                with self.subTest(command=command, failure=failure):
                    def run(arguments, **kwargs):
                        if arguments[3] == "cat-file":
                            self.assertNotIn("text", kwargs)
                            self.assertIsInstance(kwargs["input"], bytes)
                        else:
                            self.assertEqual(kwargs["encoding"], "utf-8")
                            self.assertEqual(kwargs["errors"], "replace")
                        if arguments[3] == command:
                            if isinstance(failure, OSError):
                                raise failure
                            if command == "cat-file":
                                return subprocess.CompletedProcess([], failure.returncode, b"", failure.stderr.encode("utf-8"))
                            return failure
                        return subprocess.CompletedProcess(arguments, 0, f"machines/{TEST_MACHINE}.json\n", "")

                    with mock.patch.object(installer.subprocess, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()) as stdout:
                        self.assertEqual(installer.build_bundle(options), 2)
                    self.assertIn(f"bundle refused: {copied}:", stdout.getvalue())
                    if command == "cat-file":
                        self.assertIn(f"machines/{TEST_MACHINE}.json", stdout.getvalue())
                    self.assertNotIn("bundle gate:", stdout.getvalue())
                    self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_refuses_existing_output(self):
        copied = self.copy_repo_without_local_machine_files("bundle-existing-output-repo")
        for name in ("bundle-existing.zip", "bundle-existing-directory"):
            with self.subTest(name=name):
                bundle = self.base / name
                if bundle.suffix == ".zip":
                    bundle.write_bytes(b"existing")
                    preserved = bundle
                else:
                    bundle.mkdir()
                    preserved = bundle / "existing.txt"
                    preserved.write_bytes(b"existing")
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn(f"bundle refused: bundle output must not exist or must be empty: {bundle}\n", result.stdout)
                self.assertEqual(result.stderr, "")
                self.assertEqual(preserved.read_bytes(), b"existing")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_multiple_spaces_match_single_space(self):
        copied = self.copy_repo_without_local_machine_files("bundle-space-run-repo")
        term = "bundleprobe" + "  corp"
        self.bundle_terms_file(copied).write_text(term + "\n", encoding="utf-8")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + term.replace("  ", " ") + "\n")
        bundle = self.base / "bundle-space-run"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1)
        self.assertIn(": " + term.replace("  ", " ") + "\n", result.stdout)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_terms_strip_surrounding_whitespace(self):
        copied = self.copy_repo_without_local_machine_files("bundle-strip-repo")
        term = "bundleprobe" + " whitespace"
        for index, text in enumerate(("\t# Comment\n" + term, "\t\n" + term, term + "\t", "\tsubstr:\t" + term + "\t")):
            with self.subTest(text=text):
                self.bundle_terms_file(copied).write_text(text, encoding="utf-8")
                bundle = self.base / f"bundle-strip-{index}"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertRegex(result.stdout, r"bundle gate: \d+ global terms, 1 terms from 1 domain lists, owned: none")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_missing_global_list_has_named_error(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-missing-global-repo")
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied)}
        with mock.patch.object(installer, "__file__", str(copied / "install.py")), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(installer.build_bundle(options), 2)
        self.assertEqual(stdout.getvalue(), "bundle refused: bundle-terms.txt is missing next to install.py\n")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_reads_tracked_json_once_and_merges_local_json(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-read-once-repo")
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"harvest": False}), encoding="utf-8")
        bundle = self.base / "bundle-read-once"
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied), "out": bundle, "dry_run": True}
        with (
            mock.patch.object(installer, "read_json", wraps=installer.read_json) as read,
            mock.patch.object(installer, "bundle_file_entries", wraps=installer.bundle_file_entries) as entries,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(installer.build_bundle(options), 0)
        target = copied / "machines" / f"{TEST_MACHINE}.json"
        self.assertEqual(read.call_args_list.count(mock.call(target)), 1)
        self.assertIs(entries.call_args.kwargs["harvest"], False)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_invalid_local_json_is_refused(self):
        for index, content in enumerate(('{"harvest": false,}', "[]", "null", '"text"')):
            with self.subTest(content=content):
                copied = self.copy_repo_without_local_machine_files(f"bundle-invalid-local-{index}-repo")
                relative = f"machines/{TEST_MACHINE}.local.json"
                (copied / relative).write_text(content, encoding="utf-8")
                bundle = self.base / f"bundle-invalid-local-{index}.zip"
                result = self.run_bundle(copied, bundle)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertTrue(result.stdout.startswith(f"bundle refused: {relative}:"), result.stdout)
                self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_local_harvest_false_omits_export_tooling(self):
        copied = self.copy_repo_without_local_machine_files("bundle-local-harvest-repo")
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"harvest": False}), encoding="utf-8")
        bundle = self.base / "bundle-local-harvest.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            instructions = archive.read("INSTALL.md").decode("utf-8")
        self.assertNotIn("harvest.py", names)
        self.assertFalse(any(name.startswith("claude/skills/harvest/") for name in names))
        self.assertNotIn("harvest", instructions)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_local_owns_cannot_skip_domain(self):
        copied = self.copy_repo_without_local_machine_files("bundle-local-owns-repo")
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(json.dumps({"owns": ["probe"]}), encoding="utf-8")
        term = self.bundle_terms_file(copied).read_text(encoding="utf-8")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + term)
        bundle = self.base / "bundle-local-owns"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1)

        self.assertIn(f"bundle gate: ignoring owns in machines/{TEST_MACHINE}.local.json (ownership is read from the committed machines/{TEST_MACHINE}.json)\n", result.stdout)

    def test_install_does_not_validate_owns(self):
        copied = self.copy_repo_without_local_machine_files("install-invalid-owns-repo")
        for index, value in enumerate((None, "probe", [""], [" "], [1])):
            with self.subTest(value=value):
                local = copied / "machines" / f"{TEST_MACHINE}.local.json"
                local.write_text(json.dumps({"owns": value}), encoding="utf-8")
                home = self.new_home(f"install-invalid-owns-{index}")
                result = self.run_install(home, copied)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertTrue((home / ".claude" / "local" / "machine.json").exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_machine_terms_punctuation(self):
        copied = self.copy_repo_without_local_machine_files("bundle-punctuation-repo")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        original = skill.read_text(encoding="utf-8")
        line_number = len((original + "\n").splitlines()) + 1
        for term, prefix, suffix in (
            ("." + "acme", "mail", ".com"),
            ("@" + "acme", "someone", ".io"),
            ("(" + "internal" + ")", "prefix", "suffix"),
        ):
            for content in (term.upper(), prefix + term.upper() + suffix):
                with self.subTest(term=term, content=content):
                    self.bundle_terms_file(copied).write_text(term + "\n", encoding="utf-8")
                    skill.write_text(original + "\n" + content + "\n", encoding="utf-8")
                    bundle = self.base / "bundle-punctuation"
                    result = self.run_bundle(copied, bundle)
                    self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1)
                    self.assertIn(f"GATE claude/skills/save/SKILL.md:{line_number}: {term}\n", result.stdout)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_empty_substring(self):
        copied = self.copy_repo_without_local_machine_files("bundle-empty-substring-repo")
        relative = str(self.bundle_terms_file(copied))
        for term in ("substr:", "SUBSTR: "):
            with self.subTest(term=term):
                Path(relative).write_text("# Exclusions\n\n" + term + "\n", encoding="utf-8")
                bundle = self.base / "bundle-empty-substring"
                result = self.run_bundle(copied, bundle)
                self.assert_bundle_refused(result, bundle, f"bundle refused: {relative}:3: empty term")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_nested_global_terms_file(self):
        copied = self.copy_repo_without_local_machine_files("bundle-nested-terms-repo")
        relative = "templates/bundle-terms.txt"
        (copied / relative).write_text("# Exclusions\n", encoding="utf-8")
        bundle = self.base / "bundle-nested-terms"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(
            result, bundle, f"bundle refused: terms file inside the bundle tree: {relative}", machine_terms=1
        )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_domain_tree_entries(self):
        for index, relative in enumerate((
            "claude/skills/x/bundle-terms/probe.txt",
            "claude/skills/x/bundle-terms.txt/probe.txt",
            "claude/skills/x/draftbundle-terms.txt",
            "claude/skills/x/Acme.Bundle-Terms.TXT",
            "claude/skills/x/Bundle-Terms/probe.txt",
            "templates/bundle-terms-draft.txt",
        )):
            with self.subTest(relative=relative):
                copied = self.copy_repo_without_local_machine_files(f"bundle-domain-entry-{index}-repo")
                path = copied / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# Draft terms\n", encoding="utf-8")
                bundle = self.base / f"bundle-domain-entry-{index}"
                result = self.run_bundle(copied, bundle)
                self.assert_bundle_refused(
                    result, bundle, f"bundle refused: terms file inside the bundle tree: {relative}", machine_terms=1
                )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_skips_cached_domain_files(self):
        copied = self.copy_repo_without_local_machine_files("bundle-cached-domain-repo")
        relative = "claude/skills/x/__pycache__/bundle-terms/probe.txt"
        path = copied / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe")
        bundle = self.base / "bundle-cached-domain"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertFalse((bundle / relative).exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_terms_open_errors_are_refused(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-open-error-repo")
        bundle = self.base / "bundle-open-error"
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied), "out": bundle, "dry_run": False}
        original_open = Path.open
        for path in (self.bundle_terms_file(copied), REPO / "bundle-terms.txt"):
            for error_type in (PermissionError, FileNotFoundError, IsADirectoryError, OSError):
                with self.subTest(path=path, error_type=error_type):
                    def open_file(source, *args, **kwargs):
                        if source == path:
                            raise error_type("blocked")
                        return original_open(source, *args, **kwargs)

                    with (
                        mock.patch.object(Path, "open", autospec=True, side_effect=open_file),
                        contextlib.redirect_stdout(io.StringIO()) as stdout,
                    ):
                        code = installer.build_bundle(options)
                    result = subprocess.CompletedProcess([], code, stdout.getvalue(), "")
                    self.assert_bundle_refused(result, bundle, f"bundle refused: {path}: blocked")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_directory_named_as_domain_is_refused(self):
        copied = self.copy_repo_without_local_machine_files("bundle-directory-domain-repo")
        path = self.bundle_terms_file(copied).with_name("directory.txt")
        path.mkdir()
        bundle = self.base / "bundle-directory-domain"
        result = self.run_bundle(copied, bundle)
        message = result.stdout.strip()
        self.assertTrue(message.startswith(f"bundle refused: {path}: "), message)
        self.assert_bundle_refused(result, bundle, message)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_hash_term_is_not_a_comment(self):
        copied = self.copy_repo_without_local_machine_files("bundle-hash-term-repo")
        term = "#" + "client-alpha"
        self.bundle_terms_file(copied).write_text("# Comment\n## Heading\n#\nterm:" + term + "\n", encoding="utf-8")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + term + "\n")
        bundle = self.base / "bundle-hash-term"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1)
        self.assertIn(": " + term + "\n", result.stdout)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_unicode_line_separator_splits_terms(self):
        copied = self.copy_repo_without_local_machine_files("bundle-line-separator-repo")
        first = "domainprobe" + "first"
        second = "domainprobe" + "second"
        self.bundle_terms_file(copied).write_text(first + "\u2028" + second, encoding="utf-8")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + first + "\u2028" + second + "\n")
        bundle = self.base / "bundle-line-separator"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 2 hits", machine_terms=2)
        self.assertEqual(result.stdout.count("GATE claude/skills/save/SKILL.md:"), 2)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_non_utf8_machine_terms(self):
        copied = self.copy_repo_without_local_machine_files("bundle-encoding-repo")
        relative = str(self.bundle_terms_file(copied))
        Path(relative).write_text("bundleprobe" + " exclusion\n", encoding="utf-16")
        bundle = self.base / "bundle-encoding"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, f"bundle refused: {relative}: is not UTF-8")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_control_characters(self):
        copied = self.copy_repo_without_local_machine_files("bundle-control-repo")
        terms_path = self.bundle_terms_file(copied)
        for content in (
            "acme\n".encode("utf-16-le"),
            "ac\u200bme\n".encode("utf-8"),
            b"substr:ac\x00me\n",
            "ac\u00a0me\n".encode("utf-8"),
            b"ac\tme\n",
        ):
            with self.subTest(content=content):
                terms_path.write_bytes(content)
                bundle = self.base / "bundle-control"
                result = self.run_bundle(copied, bundle)
                self.assert_bundle_refused(
                    result, bundle, f"bundle refused: {terms_path}:1: term contains a control or separator character"
                )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_deduplicates_global_and_machine_terms(self):
        copied = self.copy_repo_without_local_machine_files("bundle-dedupe-repo")
        term = "fl" + "eet"
        self.bundle_terms_file(copied).write_text(term + "\n" + term + "\n", encoding="utf-8")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + term + "\n")
        bundle = self.base / "bundle-dedupe"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=0)
        self.assertEqual(result.stdout.count("GATE claude/skills/save/SKILL.md:"), 1)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_substring_collapses_spaces(self):
        copied = self.copy_repo_without_local_machine_files("bundle-substring-spaces-repo")
        term = "acme" + " corp"
        self.bundle_terms_file(copied).write_text("substr:" + term.replace(" ", "  ") + "\n", encoding="utf-8")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        original = skill.read_text(encoding="utf-8")
        skill.write_text(original + "\n" + term + "\n", encoding="utf-8")
        bundle = self.base / "bundle-substring-spaces"
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1)
        self.assertIn(": " + term + "\n", result.stdout)
        skill.write_text(original + "\n" + term.replace(" ", "  ") + "\n", encoding="utf-8")
        result = self.run_bundle(copied, bundle)
        self.assert_bundle_refused(result, bundle, "bundle refused: 1 hits", machine_terms=1)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_deduplicated_counts_match_gate_patterns(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-pattern-counts-repo")
        term = "countprobe" + "acme"
        global_term = "globalprobe" + "acme"
        self.bundle_terms_file(copied).write_text(term + "\n" + term + "\n" + global_term + "\n", encoding="utf-8")
        second = self.bundle_terms_file(copied).with_name("second.txt")
        second.write_text(term + "\n", encoding="utf-8")
        self.declare_bundle_domains(copied, ["probe", "second"])
        bundle = self.base / "bundle-pattern-counts"
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied), "out": bundle, "dry_run": False}
        with (
            mock.patch.object(installer, "bundle_forbidden_terms", return_value=installer.parse_bundle_terms(global_term + "\n" + global_term, "test")),
            mock.patch.object(installer, "bundle_gate", wraps=installer.bundle_gate) as gate,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(installer.build_bundle(options), 0)
        self.assertIn("bundle gate: 1 global terms, 1 terms from 2 domain lists, owned: none\n", stdout.getvalue())
        self.assertEqual(len(gate.call_args.args[1]), 2)
        self.assertIn("Bundle gate applied: 1 global terms, 2 domain lists with 1 terms,", (bundle / "INSTALL.md").read_text(encoding="utf-8"))
        report = mock.Mock()
        with mock.patch.object(installer, "bundle_forbidden_terms", return_value=installer.parse_bundle_terms(global_term + "\n" + global_term, "test")):
            terms = installer.all_bundle_terms(self.bundle_home(copied), report=report)
        report.assert_called_once_with(1, 1, 2)
        self.assertEqual(len(terms), 2)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_equivalent_space_patterns_share_one_hit_and_count(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-equivalent-spaces-repo")
        term = "acme" + " corp"
        self.bundle_terms_file(copied).write_text(term.replace(" ", "  ") + "\n", encoding="utf-8")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        original = skill.read_text(encoding="utf-8")
        skill.write_text(original + "\n" + term + "\n", encoding="utf-8")
        bundle = self.base / "bundle-equivalent-spaces"
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied), "out": bundle, "dry_run": False}
        with mock.patch.object(installer, "bundle_forbidden_terms", return_value=installer.parse_bundle_terms(term, "test")):
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(installer.build_bundle(options), 2)
            self.assertIn("bundle gate: 1 global terms, 0 terms from 1 domain lists, owned: none\n", stdout.getvalue())
            self.assertEqual(stdout.getvalue().count("GATE "), 1)
            self.assertIn("bundle refused: 1 hits\n", stdout.getvalue())
            self.assertFalse(bundle.exists())
            skill.write_text(original, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(installer.build_bundle(options), 0)
        self.assertIn("Bundle gate applied: 1 global terms, 1 domain lists with 0 terms,", (bundle / "INSTALL.md").read_text(encoding="utf-8"))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_dedupe_collapses_identical_patterns_across_kinds(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        terms = installer.parse_bundle_terms("(probe)\nsubstr:(probe)\n", "test")
        self.assertEqual(terms[0][1].pattern, terms[1][1].pattern)
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            hits = installer.bundle_gate([("probe.md", b"(probe)\n")], installer.dedupe_terms(terms))
        self.assertEqual(hits, 1)
        self.assertEqual(stdout.getvalue(), "GATE probe.md:1: (probe)\n")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_dedupe_keeps_distinct_pattern_text(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        terms = installer.parse_bundle_terms("Acme\nacme\nStra\u00dfe\nStrasse\nsubstr:ACME\n", "test")
        self.assertEqual([value for value, pattern in installer.dedupe_terms(terms)], ["Acme", "acme", "Stra\u00dfe", "Strasse", "ACME"])

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_rejects_malformed_machine_json(self):
        copied = self.copy_repo_without_local_machine_files("bundle-malformed-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        for content in ("{bad json", "[1,2]", "null", "1"):
            with self.subTest(content=content):
                machine_path.write_text(content, encoding="utf-8")
                bundle = self.base / "bundle-malformed"
                result = self.run_bundle(copied, bundle)
                message = result.stdout.splitlines()[-1]
                self.assertTrue(message.startswith(f"bundle refused: machines/{TEST_MACHINE}.json: is not a JSON object ("), message)
                self.assertTrue(message.endswith(")"), message)
                self.assert_bundle_refused(result, bundle, message)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_copies_share_builder_home(self):
        first = self.copy_repo_without_local_machine_files("bundle-shared-first-repo")
        second = self.copy_repo_without_local_machine_files("bundle-shared-second-repo", terms=False)
        self.declare_bundle_domains(second, ["probe"])
        for copied in (first, second):
            with self.subTest(copied=copied):
                self.assertFalse(list((copied / "machines").glob("*.bundle-terms.txt")))
                bundle = self.base / (copied.name + "-bundle")
                result = self.run_bundle(copied, bundle, home=self.bundle_home(first))
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertRegex(result.stdout, r"bundle gate: \d+ global terms, 1 terms from 1 domain lists, owned: none")
                self.assertTrue((bundle / "INSTALL.md").is_file())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_tracked_machine_owns_schema(self):
        result = subprocess.run(["git", "ls-files", "machines/*.json"], cwd=REPO, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())
        for relative in result.stdout.splitlines():
            with self.subTest(relative=relative):
                owns = read_json_test(REPO / relative).get("owns", [])
                self.assertIsInstance(owns, list)
                self.assertTrue(all(isinstance(domain, str) and domain.strip() for domain in owns))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_gate_refuses(self):
        copied = self.copy_repo_without_local_machine_files("bundle-gate-repo")
        skill = copied / "claude" / "skills" / "save" / "SKILL.md"
        with skill.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("This mentions the " + "fl" + "eet.\n")
        bundle = self.base / "bundle-refused"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("GATE claude/skills/save/SKILL.md:", result.stdout)
        self.assertIn("bundle refused: 1 hits", result.stdout)
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_gate_scans_all_text_entries(self):
        copied = self.copy_repo_without_local_machine_files("bundle-text-gate-repo")
        skill = copied / "claude" / "skills" / "save"
        (skill / "data.yaml").write_text(
            "fleeting\n" + "fl" + "eet\n", encoding="utf-8"
        )
        (skill / "NOTICE").write_text(
            "Other  " + "machines\n", encoding="utf-8"
        )
        substring_term = "bundleprobe" + "fragment"
        with self.bundle_terms_file(copied).open("a", encoding="utf-8") as terms:
            terms.write(f"substr:{substring_term}\n")
        (skill / "account.txt").write_text(
            "prefix" + substring_term.upper() + "suffix\n", encoding="utf-8"
        )
        bundle = self.base / "bundle-text-refused"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertNotIn("GATE claude/skills/save/data.yaml:1:", result.stdout)
        self.assertIn("GATE claude/skills/save/data.yaml:2:", result.stdout)
        self.assertIn("GATE claude/skills/save/NOTICE:1:", result.stdout)
        self.assertIn("GATE claude/skills/save/account.txt:1:", result.stdout)
        self.assertIn("bundle refused: 3 hits", result.stdout)
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_gate_rejects_binary(self):
        copied = self.copy_repo_without_local_machine_files("bundle-binary-gate-repo")
        path = copied / "claude" / "skills" / "save" / "data.bin"
        path.write_bytes(b"\xff\xfe")
        bundle = self.base / "bundle-binary-refused"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn(
            "GATE claude/skills/save/data.bin: binary or non-UTF-8 content",
            result.stdout,
        )
        self.assertFalse(bundle.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_zip_carries_manifest(self):
        copied = self.copy_repo_without_local_machine_files("manifest-bundle-repo")
        bundle = self.base / "manifest-bundle.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
            manifest = archive.read("MANIFEST.txt")
        self.assertEqual(manifest, ("\n".join(names) + "\n").encode("utf-8"))
        directory = self.base / "manifest-bundle-directory"
        result = self.run_bundle(copied, directory)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual((directory / "MANIFEST.txt").read_bytes(), manifest)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_publish_has_no_manifest(self):
        copied = self.copy_repo_without_local_machine_files("manifest-publish-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine.update(publish=True, harvest=False, plan_tools=False)
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        for output in (self.base / "manifest-publish.zip", self.base / "manifest-publish-directory"):
            result = self.run_bundle(copied, output)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertIn("(65 files)", result.stdout)
            if output.suffix == ".zip":
                with zipfile.ZipFile(output) as archive:
                    self.assertNotIn("MANIFEST.txt", archive.namelist())
            else:
                self.assertFalse((output / "MANIFEST.txt").exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_zip_carries_manifest(self):
        copied = self.copy_repo_without_local_machine_files("manifest-addon-repo")
        addon = self.base / "manifest-addon.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(addon) as archive:
            names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
            manifest = archive.read("MANIFEST-addon-daily.txt")
        self.assertEqual(len(names), 5)
        self.assertEqual(manifest, ("\n".join(names) + "\n").encode("utf-8"))
        directory = self.base / "manifest-addon-directory"
        result = self.run_addon(copied, directory)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual((directory / "MANIFEST-addon-daily.txt").read_bytes(), manifest)

    def unpack_fixture(self, name):
        copied = self.copy_repo_without_local_machine_files(name + "-repo")
        local_relative = f"machines/{TEST_MACHINE}.local.md"
        (copied / local_relative).write_text("Release local fixture.\n", encoding="utf-8")
        skill = copied / "claude" / "skills" / "unpack-probe"
        skill.mkdir()
        (skill / "SKILL.md").write_text("Release skill fixture.\n", encoding="utf-8")
        first = self.base / (name + "-first.zip")
        result = self.run_bundle(copied, first)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        target = self.base / (name + "-target")
        with zipfile.ZipFile(first) as archive:
            archive.extractall(target)
            previous = archive.read("MANIFEST.txt").decode("utf-8").splitlines()
        users = {
            "HANDOFF-2026-01-01-x.md": b"User handoff.\n",
            ".scratch/note.txt": b"User scratch.\n",
            "bundles/old.zip": b"User bundle.\n",
            "inbox/x.md": b"User inbox.\n",
        }
        for relative, content in users.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (target / local_relative).write_bytes(b"Edited local marker.\n")
        shutil.rmtree(skill)
        second = self.base / (name + "-second.zip")
        result = self.run_bundle(copied, second)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return target, first, second, previous, users, local_relative

    def run_unpack(self, archive, target, extra=None):
        return subprocess.run(
            [sys.executable, str(REPO / "install.py"), "--unpack", str(archive), "--repo", str(target), *(extra or [])],
            capture_output=True, text=True, check=False,
        )

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_unpack_replaces_release_and_keeps_user_files(self):
        target, first, second, previous, users, local_relative = self.unpack_fixture("unpack-release")
        before = {relative: (target / relative).read_bytes() for relative in previous}
        result = self.run_unpack(second, target)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(second) as archive:
            manifest = archive.read("MANIFEST.txt")
            current = manifest.decode("utf-8").splitlines()
            local_content = archive.read(local_relative)
            unchanged = sum(before.get(relative) == archive.read(relative) for relative in current)
        removed = set(previous) - set(current)
        for relative in removed:
            self.assertFalse((target / relative).exists(), relative)
        self.assertFalse((target / "claude/skills/unpack-probe").exists())
        for relative, content in users.items():
            self.assertEqual((target / relative).read_bytes(), content)
        self.assertEqual((target / local_relative).read_bytes(), local_content)
        backups = list((target / ".scratch").glob("unpack-backup-*/" + local_relative))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"Edited local marker.\n")
        self.assertEqual((target / "MANIFEST.txt").read_bytes(), manifest)
        backed_up = sum(relative.casefold().endswith(".local.md") for relative in current)
        self.assertEqual(result.stdout.splitlines()[-1], f"unpacked {second} into {target}: {len(current) - unchanged} written, {unchanged} unchanged, {len(removed)} removed, {backed_up} backed up; next: python install.py --machine {TEST_MACHINE}")
        others = sum(relative.split("/")[0] not in {".scratch", "bundles", "inbox"} for relative in users)
        self.assertIn(f"kept {others} other files outside the user folders", result.stdout)
        actions = [line for line in result.stdout.splitlines() if line.startswith("ACTION")]
        self.assertIn(str(target / "MANIFEST.txt"), actions[-1])
        removal_index = next(index for index, line in enumerate(actions) if "(removed)" in line)
        self.assertFalse(any("(written)" in line for line in actions[removal_index:-1]))
        local_mtime = (target / local_relative).stat().st_mtime_ns
        mtimes = {relative: (target / relative).stat().st_mtime_ns for relative in current}
        repeated = self.run_unpack(second, target)
        self.assertEqual(repeated.returncode, 0, repeated.stderr or repeated.stdout)
        self.assertIn(str(target / local_relative) + "  (unchanged)", repeated.stdout)
        self.assertIn(f"0 written, {len(current)} unchanged, 0 removed, 0 backed up", repeated.stdout.splitlines()[-1])
        self.assertEqual((target / local_relative).stat().st_mtime_ns, local_mtime)
        self.assertEqual({relative: (target / relative).stat().st_mtime_ns for relative in current}, mtimes)
        self.assertEqual(list((target / ".scratch").glob("unpack-backup-*/" + local_relative)), backups)
        repeated_dry = self.run_unpack(second, target, ["--dry-run"])
        self.assertEqual(repeated_dry.returncode, 0, repeated_dry.stderr or repeated_dry.stdout)
        self.assertIn("(would leave unchanged)", repeated_dry.stdout)
        self.assertIn(f"0 written, {len(current)} unchanged, 0 removed, 0 backed up", repeated_dry.stdout.splitlines()[-1])
        self.assertEqual({relative: (target / relative).stat().st_mtime_ns for relative in current}, mtimes)
        protected = (
            ".SCRATCH/retired.txt", "BuNdLeS/retired.txt", "inbox/retired.txt",
            "transcripts/retired.txt", "data/retired.txt", "daily/retired.txt",
            "evals/retired.txt", ".claude/retired.txt", "nested/__pycache__/retired.txt",
            "machines/probe.local.md", "machines/probe.local.json", "nested/probe.local.json",
            "nested/HANDOFF-retired.md", "nested/pr-review-findings.json", "auto-trail.tsv", "nested/retired.PYC",
        )
        dropped = ("evals/README.md", "nested/ordinary.local.md")
        for relative in (*protected, *dropped):
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"Retired fixture.\n")
        addon_manifest = "MANIFEST-addon-fixture.txt"
        (target / addon_manifest).write_text("\n".join(sorted([addon_manifest, *protected, *dropped])) + "\n", encoding="utf-8")
        addon = self.base / "unpack-protected-addon.zip"
        with zipfile.ZipFile(addon, "w") as archive:
            archive.writestr("ADDON.md", "Addon fixture.\n")
            archive.writestr(addon_manifest, "ADDON.md\n" + addon_manifest + "\n")
        result = self.run_unpack(addon, target)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        for relative in protected:
            with self.subTest(protected=relative):
                self.assertEqual((target / relative).read_bytes(), b"Retired fixture.\n")
        for relative in dropped:
            self.assertFalse((target / relative).exists())
        self.assertEqual(result.stdout.count("(kept (user file))"), len(protected))
        self.assertIn(f"{len(dropped)} removed, 0 backed up", result.stdout.splitlines()[-1])
        self.assertEqual(list((target / ".scratch").glob("unpack-backup-*/" + local_relative)), backups)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_unpack_dry_run_writes_nothing(self):
        target, first, second, previous, users, local_relative = self.unpack_fixture("unpack-dry")
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for relative in ("claude/CLAUDE.md", "claude/keybindings.json"):
            (target / relative).write_bytes(b"Old release interruption fixture.\n")
        before = installer.tree_snapshot(target)
        result = self.run_unpack(second, target, ["--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(installer.tree_snapshot(target), before)
        self.assertIn("(would remove)", result.stdout)
        self.assertIn("(would back up)", result.stdout)
        self.assertIn("(would write)", result.stdout)
        self.assertFalse(list((target / ".scratch").glob("unpack-backup-*")))
        visited = []
        original_walk = os.walk

        def observed_walk(root):
            for record in original_walk(root):
                visited.append(Path(record[0]).relative_to(target).parts)
                yield record

        with mock.patch.object(installer.os, "walk", side_effect=observed_walk), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["--unpack", str(second), "--repo", str(target), "--dry-run"]), 0)
        excluded = {".git", ".scratch", "bundles", "inbox", "transcripts", "data", "daily", "__pycache__"}
        self.assertFalse(any(parts and parts[0].casefold() in excluded for parts in visited))
        self.assertFalse(any(part.casefold() in {".git", "__pycache__"} for parts in visited for part in parts))
        self.assertEqual(installer.tree_snapshot(target), before)
        replacements = []
        original_replace = os.replace

        def interrupted_replace(source, destination):
            replacements.append(Path(destination))
            if len(replacements) == 2:
                raise OSError("Interrupted replacement fixture")
            return original_replace(source, destination)

        old_manifest = (target / "MANIFEST.txt").read_bytes()
        with mock.patch.object(installer.os, "replace", side_effect=interrupted_replace), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["--unpack", str(second), "--repo", str(target)]), 2)
        self.assertEqual(len(replacements), 2)
        self.assertEqual(replacements[1].read_bytes(), before[replacements[1].relative_to(target).as_posix()])
        self.assertEqual((target / "MANIFEST.txt").read_bytes(), old_manifest)
        self.assertTrue((target / "claude/skills/unpack-probe/SKILL.md").is_file())
        self.assertFalse(list(target.rglob("*.tmp")))
        recovered = self.run_unpack(second, target)
        self.assertEqual(recovered.returncode, 0, recovered.stderr or recovered.stdout)
        self.assertFalse((target / "claude/skills/unpack-probe").exists())

    def test_unpack_write_file_mode_policy(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        cases = (
            ("posix", True, 0o751, 0o100666, 0o751),
            ("posix", True, 0o640, 0o100777, 0o640),
            ("posix", False, 0, 0o100100, 0o751),
            ("posix", False, 0, 0o100001, 0o751),
            ("posix", False, 0, 0o100666, None),
            ("posix", False, 0, 0, None),
            ("nt", True, 0o751, 0o100777, None),
        )
        for platform, exists, current_mode, zip_mode, expected in cases:
            with self.subTest(platform=platform, exists=exists, current_mode=current_mode, zip_mode=zip_mode):
                destination = mock.Mock()
                destination.name = "mode-fixture.txt"
                destination.exists.return_value = exists
                destination.stat.return_value = mock.Mock(st_mode=0o100000 | current_mode)
                temporary = destination.with_name.return_value
                temporary.stat.return_value = mock.Mock(st_mode=0o100640)
                with mock.patch.object(installer.os, "name", platform), mock.patch.object(installer.os, "replace") as replace:
                    installer.unpack_write_file(destination, b"Mode fixture.\n", zip_mode)
                    replace.assert_called_once_with(temporary, destination)
                temporary.write_bytes.assert_called_once_with(b"Mode fixture.\n")
                if expected is None:
                    temporary.chmod.assert_not_called()
                else:
                    temporary.chmod.assert_called_once_with(expected)
                temporary.unlink.assert_called_once_with(missing_ok=True)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_unpack_manifest_metadata_is_lenient(self):
        target, first, second, previous, users, local_relative = self.unpack_fixture("unpack-metadata")
        owner = "MaNiFeSt-AdDoN-FiXtUrE.TxT"
        owned_relative = "ordinary-owned.txt"
        (target / owned_relative).write_bytes(b"Owned fixture.\n")
        previous_path = target / "MANIFEST.txt"
        previous_path.write_bytes(("\n".join([*previous, "", "../escape-previous.txt", owned_relative]) + "\n").encode("utf-8"))
        (target / owner).write_bytes(("\n".join(["", "../escape-owner.txt", owned_relative, owned_relative, owner]) + "\n").encode("utf-8"))
        unknown = {**users, "manifest-notes.txt": b"claude/skills/unpack-probe/SKILL.md\n"}
        for directory in (".scratch", "bundles", "inbox", "transcripts", "data", "daily"):
            unknown[f"{directory}/count-probe.txt"] = b"Top level user folder.\n"
            unknown[f"project/{directory}/count-probe.txt"] = b"Nested ordinary folder.\n"
        for relative in ("project/.git/count-probe.txt", "project/cache/__pycache__/count-probe.txt", "__pycache__/count-probe.txt"):
            unknown[relative] = b"Excluded at every depth.\n"
        for relative, content in unknown.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        before = installer.tree_snapshot(target)
        dry = self.run_unpack(second, target, ["--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr or dry.stdout)
        self.assertEqual(installer.tree_snapshot(target), before)
        self.assertIn("(would read other manifest)", dry.stdout)
        self.assertEqual(dry.stdout.count("ignoring line"), 4)
        result = self.run_unpack(second, target)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        for line_number in (len(previous) + 1, len(previous) + 2):
            self.assertIn(f"note: {previous_path}: ignoring line {line_number}", result.stdout)
        for line_number in (1, 2):
            self.assertIn(f"note: {target / owner}: ignoring line {line_number}", result.stdout)
        self.assertEqual(result.stdout.count("ignoring line"), 4)
        self.assertNotIn(str(target / "manifest-notes.txt"), result.stdout)
        self.assertIn(f"(kept (owned by {owner}))", result.stdout)
        self.assertEqual((target / owned_relative).read_bytes(), b"Owned fixture.\n")
        self.assertFalse((target / "claude/skills/unpack-probe").exists())
        for relative, content in unknown.items():
            self.assertEqual((target / relative).read_bytes(), content)
        excluded = {".git", ".scratch", "bundles", "inbox", "transcripts", "data", "daily", "__pycache__"}
        others = sum(
            relative.split("/")[0].casefold() not in excluded
            and not any(part.casefold() in {".git", "__pycache__"} for part in relative.split("/")[:-1])
            for relative in unknown
        )
        self.assertIn(f"kept {others} other files outside the user folders", result.stdout)
        strict = self.base / "unpack-strict-manifest.zip"
        manifest = "MANIFEST-addon-strict.txt"
        with zipfile.ZipFile(strict, "w") as archive:
            archive.writestr("ADDON.md", b"Addon fixture.\n")
            archive.writestr(manifest, "\n".join(["ADDON.md", manifest, "", "../escape-zip.txt"]) + "\n")
        before = installer.tree_snapshot(target)
        result = self.run_unpack(strict, target)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("path outside the install folder", result.stdout)
        self.assertEqual(installer.tree_snapshot(target), before)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    @unittest.skipIf(os.name == "nt", "executable permissions require a non Windows platform")
    def test_unpack_preserves_posix_modes(self):
        target = self.base / "unpack-modes-target"
        target.mkdir()
        (target / "install.py").write_bytes(b"Install fixture.\n")
        existing = {"claude/statusline-command.sh": 0o751, "templates/launchd-codex-launcher.sh": 0o700}
        for relative, mode in existing.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"Old executable fixture.\n")
            path.chmod(mode)
        modes = {
            "claude/statusline-command.sh": 0o100666,
            "templates/launchd-codex-launcher.sh": 0o100777,
            "new-executable.sh": 0o100100,
            "new-ordinary.txt": 0o100666,
        }
        addon = self.base / "unpack-modes.zip"
        manifest = "MANIFEST-addon-modes.txt"
        with zipfile.ZipFile(addon, "w") as archive:
            for relative, mode in modes.items():
                info = zipfile.ZipInfo(relative)
                info.external_attr = mode << 16
                archive.writestr(info, b"New mode fixture.\n")
            archive.writestr("ADDON.md", b"Addon fixture.\n")
            archive.writestr(manifest, "\n".join(sorted([*modes, "ADDON.md", manifest])) + "\n")
        previous_umask = os.umask(0o027)
        try:
            result = self.run_unpack(addon, target)
        finally:
            os.umask(previous_umask)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        expected = {**existing, "new-executable.sh": 0o751, "new-ordinary.txt": 0o640}
        for relative, mode in expected.items():
            self.assertEqual((target / relative).stat().st_mode & 0o777, mode)
            self.assertEqual((target / relative).read_bytes(), b"New mode fixture.\n")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_unpack_keeps_files_owned_by_addon_manifest(self):
        copied = self.copy_repo_without_local_machine_files("unpack-owned-repo")
        first = self.base / "unpack-owned-first.zip"
        result = self.run_bundle(copied, first)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        addon = self.base / "unpack-owned-addon.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        target = self.base / "unpack-owned-target"
        for bundle in (first, addon):
            with zipfile.ZipFile(bundle) as archive:
                archive.extractall(target)
        relative = "tools/daily_collect.py"
        content = (target / relative).read_bytes()
        (copied / relative).unlink()
        second = self.base / "unpack-owned-second.zip"
        result = self.run_bundle(copied, second)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        result = self.run_unpack(second, target)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual((target / relative).read_bytes(), content)
        self.assertIn(str(target / relative) + "  (kept (owned by MANIFEST-addon-daily.txt))", result.stdout)
        self.assertIn("0 removed, 0 backed up", result.stdout.splitlines()[-1])

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    @unittest.skipIf(os.name == "nt", "unsafe file names require a non Windows platform")
    def test_bundle_refuses_names_unpack_cannot_accept(self):
        for index, filename in enumerate(("unsafe:name.md", "unsafe-name.md.")):
            with self.subTest(filename=filename):
                copied = self.copy_repo_without_local_machine_files(f"unpack-invalid-{index}-repo")
                relative = "claude/skills/daily/" + filename
                (copied / relative).write_text("Unsafe name fixture.\n", encoding="utf-8")
                for kind, build in (("bundle", self.run_bundle), ("addon", self.run_addon)):
                    output = self.base / f"unpack-invalid-{index}-{kind}.zip"
                    result = build(copied, output)
                    self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                    self.assertIn(f"{relative}: name would be refused by --unpack", result.stdout)
                    self.assertFalse(output.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_unpack_refuses_clone_zip_slip_and_missing_manifest(self):
        target, first, second, previous, users, local_relative = self.unpack_fixture("unpack-refusal")
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        (target / ".git").mkdir()
        before = installer.tree_snapshot(target)
        result = self.run_unpack(second, target)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("is a git clone; update it with git pull", result.stdout)
        self.assertEqual(installer.tree_snapshot(target), before)
        copied = self.copy_repo_without_local_machine_files("unpack-addon-clone-repo")
        addon = self.base / "unpack-addon-clone.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        result = self.run_unpack(addon, target)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue((target / "MANIFEST-addon-daily.txt").is_file())
        self.assertTrue((target / ".git").is_dir())
        self.assertEqual((target / "MANIFEST.txt").read_text(encoding="utf-8").splitlines(), previous)
        self.assertTrue(result.stdout.rstrip().endswith("next: python install.py"))
        before = installer.tree_snapshot(target)
        for index, path in enumerate(("../escape.txt", "/escape.txt", "C:/escape.txt", r"nested\..\escape.txt", "unlisted.txt")):
            with self.subTest(path=path):
                unsafe = self.base / f"unpack-unsafe-{index}.zip"
                with zipfile.ZipFile(unsafe, "w") as archive:
                    archive.writestr("MANIFEST.txt", "MANIFEST.txt\n")
                    archive.writestr(path, "Unsafe fixture.\n")
                result = self.run_unpack(unsafe, target)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                message = "not in the manifest" if path == "unlisted.txt" else "path outside the install folder"
                with zipfile.ZipFile(unsafe) as archive:
                    entry_name = archive.infolist()[-1].filename
                self.assertIn(f"{entry_name}: {message}", result.stdout)
                self.assertEqual(installer.tree_snapshot(target), before)
                self.assertFalse((target.parent / "escape.txt").exists())
        missing = self.base / "unpack-missing.zip"
        with zipfile.ZipFile(missing, "w") as archive:
            archive.writestr("fixture.txt", "Legacy fixture.\n")
        result = self.run_unpack(missing, target)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("carries no manifest; it predates the unpack step", result.stdout)
        self.assertEqual(installer.tree_snapshot(target), before)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_unpack_without_previous_manifest_notes_and_removes_nothing(self):
        target, first, second, previous, users, local_relative = self.unpack_fixture("unpack-legacy")
        legacy = self.base / "unpack-legacy.zip"
        with zipfile.ZipFile(first) as source, zipfile.ZipFile(legacy, "w") as archive:
            for info in source.infolist():
                if info.filename != "MANIFEST.txt":
                    archive.writestr(info, source.read(info))
        legacy_target = self.base / "unpack-legacy-target-no-manifest"
        with zipfile.ZipFile(legacy) as archive:
            archive.extractall(legacy_target)
        result = self.run_unpack(second, legacy_target)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn(f"note: no previous manifest in {legacy_target}; files the previous release shipped and this one does not cannot be detected and stay in place", result.stdout)
        self.assertTrue((legacy_target / "claude/skills/unpack-probe/SKILL.md").is_file())
        self.assertIn("0 removed", result.stdout.splitlines()[-1])
        self.assertTrue((legacy_target / "MANIFEST.txt").is_file())

    def test_unpack_option_combinations_are_refused(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for option in ("machine", "bundle", "addon", "out", "git-hooks"):
            with self.subTest(option=option):
                with self.assertRaisesRegex(ValueError, f"--unpack cannot be combined with --{option}"):
                    installer.parse_args(["--unpack", "x.zip", "--" + option, "m"])
        with self.assertRaisesRegex(ValueError, "missing value for --unpack"):
            installer.parse_args(["--unpack"])
        options = installer.parse_args(["--unpack", "x.zip", "--repo", str(self.base), "--dry-run"])
        self.assertEqual(options["repo"], self.base)
        self.assertTrue(options["dry_run"])

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_install_text_names_unpack(self):
        copied = self.copy_repo_without_local_machine_files("unpack-text-repo")
        bootstrap = "If the install folder's install.py does not know `--unpack` yet, extract install.py from the zip first (`unzip -o <zip> install.py` or Expand-Archive for that one file) and run that one."
        bundle = self.base / "unpack-text-bundle.zip"
        result = self.run_bundle(copied, bundle)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(bundle) as archive:
            self.assertIn("install.py --unpack", archive.read("INSTALL.md").decode("utf-8"))
            self.assertIn(bootstrap, archive.read("INSTALL.md").decode("utf-8"))
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["publish"] = True
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        published = self.base / "unpack-text-published.zip"
        result = self.run_bundle(copied, published)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(published) as archive:
            self.assertNotIn("install.py --unpack", archive.read("INSTALL.md").decode("utf-8"))
            self.assertNotIn(bootstrap, archive.read("INSTALL.md").decode("utf-8"))
        addon = self.base / "unpack-text-addon.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        with zipfile.ZipFile(addon) as archive:
            self.assertIn("install.py --unpack", archive.read("ADDON.md").decode("utf-8"))
            self.assertNotIn(bootstrap, archive.read("ADDON.md").decode("utf-8"))
            self.assertIn("If the install folder's install.py does not know `--unpack` yet, unzip this addon by hand this once; a harness bundle brings the newer install.py.", archive.read("ADDON.md").decode("utf-8"))

    def run_addon(self, copied, output, name="daily", extra=None):
        command = [
            sys.executable, str(REPO / "install.py"), "--addon", name,
            "--out", str(output), "--repo", str(copied), "--home", str(self.bundle_home(copied)),
        ]
        if extra:
            command.extend(extra)
        return subprocess.run(command, capture_output=True, text=True, check=False)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_daily_zip(self):
        copied = self.copy_repo_without_local_machine_files("addon-zip-repo")
        addon = self.base / "addon.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("addon gate:", result.stdout)
        self.assertIn("addon written", result.stdout)
        self.assertIn("(5 files)", result.stdout)
        with zipfile.ZipFile(addon) as archive:
            names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
            instructions = archive.read("ADDON.md").decode("utf-8")
        self.assertEqual(names, ["ADDON.md", "MANIFEST-addon-daily.txt", "claude/skills/daily/SKILL.md", "tools/daily_collect.py", "tools/daily_collect_test.py"])
        self.assertTrue(instructions.startswith("# Daily addon\n\nBuilt "))
        self.assertIn("from an uncommitted tree.", instructions)
        fingerprint = hashlib.sha256(self.bundle_terms_file(copied).read_bytes()).hexdigest()[:8]
        self.assertRegex(instructions, r"Addon gate applied: \d+ global terms, 1 domain lists with 1 terms, fingerprints " + fingerprint)
        for step in ("1. Unzip this file", "2. Add a `daily` object", "3. Run `python install.py`", "4. Restart Claude Code and type `daily`."):
            self.assertIn(step, instructions)
        self.assertIn('"daily": {"output": "<folder for the summaries>"}', instructions)
        self.assertIn("~/.claude/local/machine.json", instructions)
        self.assertIn("Update: run `python install.py --unpack <zip>`", instructions)
        self.assertIn("Only the files listed above and this ADDON.md are written.", instructions)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_usage_zip(self):
        copied = self.copy_repo_without_local_machine_files("addon-usage-repo")
        addon = self.base / "addon-usage.zip"
        result = self.run_addon(copied, addon, name="usage")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("addon gate:", result.stdout)
        self.assertIn("addon written", result.stdout)
        self.assertIn("(10 files)", result.stdout)
        with zipfile.ZipFile(addon) as archive:
            names = sorted(info.filename for info in archive.infolist() if not info.is_dir())
            instructions = archive.read("ADDON.md").decode("utf-8")
        self.assertEqual(names, sorted([
            "ADDON.md",
            "MANIFEST-addon-usage.txt",
            "monitor/local.py",
            "monitor/local.html",
            "monitor/heartbeat.py",
            "monitor/local_test.py",
            "monitor/heartbeat_test.py",
            "monitor/fixtures/proxy-credentials.json",
            "monitor/USAGE.md",
            "claude/skills/usage/SKILL.md",
        ]))
        self.assertTrue(instructions.startswith("# Usage addon\n\nBuilt "))
        for step in ("1. Unzip this file", "2. Run `python install.py`", "3. Restart Claude Code and type `usage`."):
            self.assertIn(step, instructions)
        self.assertIn("http://127.0.0.1:9000", instructions)
        self.assertIn("No key in `machine.local.json` is needed.", instructions)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_dry_run_writes_nothing(self):
        copied = self.copy_repo_without_local_machine_files("addon-dry-repo")
        addon = self.base / "addon-dry-parent" / "addon.zip"
        result = self.run_addon(copied, addon, extra=["--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("addon would write {} (5 files)".format(addon), result.stdout)
        self.assertFalse(addon.parent.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_unknown_is_refused(self):
        copied = self.copy_repo_without_local_machine_files("addon-unknown-repo")
        addon = self.base / "addon-unknown.zip"
        result = self.run_addon(copied, addon, name="missing")
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertEqual(result.stdout, "addon refused: unknown addon missing; available addons: daily, usage\n")
        self.assertFalse(addon.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_output_exists_is_refused(self):
        copied = self.copy_repo_without_local_machine_files("addon-existing-repo")
        for directory in (False, True):
            with self.subTest(directory=directory):
                addon = self.base / ("addon-existing-directory" if directory else "addon-existing.zip")
                if directory:
                    addon.mkdir()
                else:
                    addon.write_bytes(b"keep")
                result = self.run_addon(copied, addon)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn("addon refused: addon output must not exist: {}".format(addon), result.stdout)
                self.assertEqual(list(addon.iterdir()) if directory else addon.read_bytes(), [] if directory else b"keep")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_cannot_combine_with_bundle_or_machine(self):
        copied = self.copy_repo_without_local_machine_files("addon-combined-repo")
        addon = self.base / "addon-combined.zip"
        for option, value in (("--bundle", TEST_MACHINE), ("--machine", TEST_MACHINE), ("--git-hooks", str(copied))):
            with self.subTest(option=option):
                result = self.run_addon(copied, addon, extra=[option, value])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--addon cannot be combined with " + option, result.stderr)
                self.assertIn("usage: python install.py", result.stderr)
                self.assertFalse(addon.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_gate_refuses_terms(self):
        copied = self.copy_repo_without_local_machine_files("addon-gate-repo")
        term = "addonprobe" + " exclusion"
        self.bundle_terms_file(copied).write_text(term + "\n", encoding="utf-8")
        skill = copied / "claude/skills/daily/SKILL.md"
        original = skill.read_text(encoding="utf-8")
        line_number = len((original + "\n").splitlines()) + 1
        skill.write_text(original + "\n" + term + "\n", encoding="utf-8")
        addon = self.base / "addon-gate.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("GATE claude/skills/daily/SKILL.md:{}: {}".format(line_number, term), result.stdout)
        self.assertIn("addon refused: 1 hits", result.stdout)
        self.assertFalse(addon.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_installs_over_a_bundle_tree(self):
        copied = self.copy_repo_without_local_machine_files("addon-source-repo")
        addon = self.base / "addon-install.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        (copied / "machines" / (TEST_MACHINE + ".local.json")).write_text(json.dumps({"plan_tools": False}), encoding="utf-8")
        target = self.base / "addon-target-bundle"
        result = self.run_bundle(copied, target)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        for relative in ("claude/skills/daily", "tools/daily_collect.py", "tools/daily_collect_test.py"):
            self.assertFalse((target / relative).exists(), relative)
        with zipfile.ZipFile(addon) as archive:
            archive.extractall(target)
        home = self.new_home("addon-install-home")
        with mock.patch.dict(globals(), REPO=target):
            installed = self.run_install(home, target)
        self.assertEqual(installed.returncode, 0, installed.stderr or installed.stdout)
        destination = home / ".claude/skills/daily/SKILL.md"
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.read_bytes(), (copied / "claude/skills/daily/SKILL.md").read_bytes())
        tested = subprocess.run(
            [sys.executable, str(target / "tools/daily_collect_test.py")], cwd=target,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        self.assertEqual(tested.returncode, 0, tested.stderr or tested.stdout)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_requires_every_declared_domain_list(self):
        copied = self.copy_repo_without_local_machine_files("addon-required-domain-repo")
        self.declare_bundle_domains(copied, ["probe", "missing"])
        addon = self.base / "addon-required-domain.zip"
        result = self.run_addon(copied, addon)
        bundle = self.run_bundle(copied, self.base / "addon-required-domain-bundle")
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertEqual(bundle.returncode, 2, bundle.stderr or bundle.stdout)
        self.assertEqual(result.stdout, bundle.stdout.replace("bundle refused:", "addon refused:"))
        self.assertIn("domain missing is owned by domain-owner but ", result.stdout)
        self.assertIn("does not exist", result.stdout)
        self.assertFalse(addon.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_missing_paths_are_refused(self):
        for index, relative in enumerate(("tools/daily_collect.py", "claude/skills/daily/")):
            with self.subTest(relative=relative):
                copied = self.copy_repo_without_local_machine_files("addon-missing-path-{}-repo".format(index))
                source = copied / relative
                self.assertTrue(source.resolve().is_relative_to(self.base.resolve()))
                if source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
                addon = self.base / "addon-missing-path-{}.zip".format(index)
                result = self.run_addon(copied, addon)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn("addon refused: {} is missing".format(relative), result.stdout)
                self.assertFalse(addon.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_empty_tree_is_refused(self):
        copied = self.copy_repo_without_local_machine_files("addon-empty-tree-repo")
        source = copied / "claude/skills/daily"
        self.assertTrue(source.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(source)
        source.mkdir()
        addon = self.base / "addon-empty-tree.zip"
        for cached in (False, True):
            with self.subTest(cached=cached):
                if cached:
                    (source / "ignored.pyc").write_bytes(b"cached")
                result = self.run_addon(copied, addon)
                self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
                self.assertIn("addon refused: claude/skills/daily/ holds no files", result.stdout)
                self.assertFalse(addon.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_and_bundle_accept_uppercase_zip_suffix(self):
        copied = self.copy_repo_without_local_machine_files("addon-uppercase-repo")
        for name, build in (("addon", self.run_addon), ("bundle", self.run_bundle)):
            with self.subTest(name=name):
                output = self.base / (name + ".release.ZIP")
                result = build(copied, output)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertTrue(zipfile.is_zipfile(output))
                self.assertIn(output.name, [path.name for path in output.parent.iterdir()])

    def test_addon_install_text_uses_named_steps(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        addon = {"description": "Fixture addon.", "paths": ("fixture.txt",), "steps": ("1. TODO: configure the fixture addon.",)}
        with mock.patch.dict(installer.ADDONS, sample=addon):
            text = installer.addon_install_text("sample", "2026-09-22", "fixture", "Fixture gate record")
        self.assertIn("# Sample addon", text)
        self.assertIn(addon["steps"][0], text)
        self.assertNotIn("daily", text)

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_requires_domain_lists(self):
        copied = self.copy_repo_without_local_machine_files("addon-no-domains-repo", terms=False)
        addon = self.base / "addon-no-domains.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("addon refused: no domain lists under ", result.stdout)
        self.assertIn("(one file per engagement or product family, see README)", result.stdout)
        self.assertFalse(addon.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_directory_skips_cached_files_and_refuses_terms(self):
        copied = self.copy_repo_without_local_machine_files("addon-tree-repo")
        skill = copied / "claude/skills/daily"
        cache = skill / "__pycache__"
        cache.mkdir()
        (cache / "bundle-terms.txt").write_text("ignored", encoding="utf-8")
        (skill / "ignored.pyc").write_bytes(b"\xff")
        addon = self.base / "addon-tree"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(len([path for path in addon.rglob("*") if path.is_file()]), 5)
        (skill / "bundle-terms.txt").write_text("refuse", encoding="utf-8")
        refused = self.base / "addon-tree-refused"
        result = self.run_addon(copied, refused)
        self.assertEqual(result.returncode, 2, result.stderr or result.stdout)
        self.assertIn("terms file inside the bundle tree: claude/skills/daily/bundle-terms.txt", result.stdout)
        self.assertFalse(refused.exists())

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_addon_applies_all_domains_and_deduplicates_terms(self):
        copied = self.copy_repo_without_local_machine_files("addon-domains-repo")
        term = "addonprobe" + " exclusion"
        terms = self.bundle_terms_file(copied)
        terms.write_text(term + "\n" + term + "\n", encoding="utf-8")
        second = terms.with_name("second.txt")
        second.write_text(term + "\n", encoding="utf-8")
        machine_path = copied / "machines" / (TEST_MACHINE + ".json")
        machine = read_json_test(machine_path)
        machine["owns"] = ["probe", "second"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        addon = self.base / "addon-domains.zip"
        result = self.run_addon(copied, addon)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertRegex(result.stdout, r"addon gate: \d+ global terms, 1 terms from 2 domain lists")
        fingerprints = ", ".join(sorted(hashlib.sha256(path.read_bytes()).hexdigest()[:8] for path in (terms, second)))
        with zipfile.ZipFile(addon) as archive:
            self.assertIn("fingerprints " + fingerprints, archive.read("ADDON.md").decode("utf-8"))

    def assert_gate_matches_reference(self, source, content, expected_hits):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        terms = installer.parse_bundle_terms(source, "fixture")
        entries = [("fixture.txt", content)]
        hits = 0
        with contextlib.redirect_stdout(io.StringIO()) as reference:
            for relative, data in entries:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    print(f"GATE {relative}: binary or non-UTF-8 content")
                    hits += 1
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    for term, pattern in terms:
                        if pattern.search(line):
                            print(f"GATE {relative}:{number}: {term}")
                            hits += 1
        with contextlib.redirect_stdout(io.StringIO()) as output:
            actual = installer.bundle_gate(entries, terms)
        self.assertEqual(hits, expected_hits)
        self.assertEqual((actual, output.getvalue()), (hits, reference.getvalue()))

    def test_gate_prefilter_whole_word(self):
        self.assert_gate_matches_reference("probe", b"probes probe unprobed", 1)

    def test_gate_prefilter_multiword_tab_and_split_lines(self):
        self.assert_gate_matches_reference("alpha beta", b"alpha\tbeta\nalpha\nbeta", 1)

    def test_gate_prefilter_uppercase(self):
        self.assert_gate_matches_reference("probe", b"PROBE", 1)

    def test_gate_prefilter_dotted_i(self):
        self.assert_gate_matches_reference("intel", "\u0130ntel".encode("utf-8"), 1)

    def test_gate_prefilter_dotless_i(self):
        self.assert_gate_matches_reference("intel", "\u0131ntel".encode("utf-8"), 1)

    def test_gate_prefilter_kelvin_sign(self):
        self.assert_gate_matches_reference("kelvin", "\u212aelvin".encode("utf-8"), 1)

    def test_gate_prefilter_long_s(self):
        self.assert_gate_matches_reference("sale", "\u017fale".encode("utf-8"), 1)

    def test_gate_prefilter_non_ascii_term(self):
        self.assert_gate_matches_reference("\u00e9\u00e9", "\u00c9\u00c9".encode("utf-8"), 1)

    def test_gate_prefilter_non_utf8(self):
        self.assert_gate_matches_reference("probe", b"\xff", 1)

    def test_gate_prefilter_no_hits(self):
        self.assert_gate_matches_reference("probe\nalpha beta", b"unrelated content", 0)

    def test_gate_prefilter_preserves_line_and_term_order(self):
        self.assert_gate_matches_reference("probe\nsubstr:x-y\nalpha beta", b"alpha beta probe prefix-x-y-suffix\nprobe x-y", 5)

    def test_gate_fragment_uses_longest_ascii_run(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.assertEqual(installer.gate_fragment("alpha beta-gamma"), "beta-gamma")
        self.assertIsNone(installer.gate_fragment("\u00e9\u00e9"))
        self.assertEqual(installer.gate_fragment("\u00e9ABC\u00e9de"), "abc")

    def test_committed_machine_texts_parses_sizes_and_missing(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        content = b"\xef\xbb\xbf{\nheader blob 123\n\xff}\n"
        batch = b"abc blob " + str(len(content)).encode("ascii") + b"\n" + content + b"\nHEAD:machines/absent.json missing\n"
        result = subprocess.CompletedProcess([], 0, stdout=batch, stderr=b"")
        with mock.patch.object(installer.subprocess, "run", return_value=result) as run:
            texts = installer.committed_machine_texts(REPO, ["probe", "absent"])
        self.assertEqual(texts, {"probe": "{\nheader blob 123\n\ufffd}\n", "absent": None})
        run.assert_called_once_with(
            ["git", "-C", str(REPO), "cat-file", "--batch"],
            input=b"HEAD:machines/probe.json\nHEAD:machines/absent.json\n",
            capture_output=True, check=False,
        )

    def test_committed_machine_texts_reports_process_failures(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for failure in (OSError("unavailable"), subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"unavailable")):
            with self.subTest(failure=failure), mock.patch.object(installer.subprocess, "run", side_effect=[failure]):
                with self.assertRaisesRegex(installer.BundleRefusal, "cannot read committed machines/probe.json: unavailable"):
                    installer.committed_machine_texts(REPO, ["probe"])

    def test_machine_owns_refuses_missing_text(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with mock.patch.object(Path, "exists", return_value=True):
            self.assertRaisesRegex(installer.BundleRefusal, "cannot read committed machines/probe.json", installer.machine_owns, REPO, "probe", texts={})

    def test_committed_machine_process_failure_names_every_request(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for failure in (OSError("unavailable"), subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"unavailable")):
            with self.subTest(failure=failure), mock.patch.object(installer.subprocess, "run", side_effect=[failure]):
                with self.assertRaises(installer.BundleRefusal) as raised:
                    installer.committed_machine_texts(REPO, ["probe", "second"])
            self.assertEqual(str(raised.exception), f"{REPO}: cannot read committed machines/probe.json, machines/second.json: unavailable")

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle creation requires repository terms")
    def test_bundle_dry_run_reads_ownership_in_one_batch(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        copied = self.copy_repo_without_local_machine_files("bundle-batch-repo")
        self.initialize_bundle_git(copied)
        options = {"bundle": TEST_MACHINE, "repo": copied, "home": self.bundle_home(copied), "out": self.base / "batch.zip", "dry_run": True}
        with mock.patch.object(installer.subprocess, "run", wraps=installer.subprocess.run) as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(installer.build_bundle(options), 0)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(sum("cat-file" in command for command in commands), 1)
        self.assertEqual(sum("show" in command for command in commands), 0)
        global_count = len(installer.dedupe_terms(installer.bundle_forbidden_terms()))
        self.assertIn(f"bundle gate: {global_count} global terms, 1 terms from 1 domain lists, owned: none\n", output.getvalue())
        self.assertIn("bundle would write ", output.getvalue())
        self.assertFalse(options["out"].exists())

    def test_custom_home_skips_absolute_machine_path(self):
        home = self.new_home("absolute-path-home")
        copied = self.copy_repo_without_local_machine_files("absolute-path-repo")
        outside = self.base / "absolute-path-target.json"
        original = b'{"permissions":{"allow":["Danger(rule)"]}}\n'
        outside.write_bytes(original)
        override = {
            "remove_allow_rules": [
                {"file": str(outside.resolve()), "match": "Danger"}
            ]
        }
        (copied / "machines" / f"{TEST_MACHINE}.local.json").write_text(
            json.dumps(override), encoding="utf-8"
        )
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outside.read_bytes(), original)
        self.assertIn("skipped (absolute path under a custom home)", result.stdout)

    def test_manifest_deletions_are_machine_scoped(self):
        copied = self.copy_repo_without_local_machine_files("machine-delete-schema-repo")
        self.assertEqual(read_json_test(copied / "install-manifest.json")["delete"], [])
        for path in (copied / "machines").glob("*.json"):
            with self.subTest(machine=path.stem):
                machine = read_json_test(path)
                if "delete" in machine:
                    self.assertIsInstance(machine["delete"], list)
                    self.assertTrue(all(isinstance(item, str) and item.startswith(".claude/") for item in machine["delete"]))

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "historical deletions require the source repository")
    def test_tracked_machine_retains_historical_skill_deletion(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.assertTrue(any(
            ".claude/skills/unslop" in read_json_test(REPO / "machines" / f"{name}.json").get("delete", [])
            for name in installer.tracked_machine_names(REPO)
        ))

    def test_empty_merged_delete_list_preserves_own_skills(self):
        home = self.new_home("empty-delete-home")
        copied = self.copy_repo_without_local_machine_files("empty-delete-repo")
        local_path = home / ".claude" / "local" / "machine.local.json"
        local_path.parent.mkdir(parents=True)
        local_path.write_text(json.dumps({"delete": []}), encoding="utf-8")
        planted = []
        for name in ("own-skill", "unslop", "checkpoint", "audit"):
            path = home / ".claude" / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"personal skill\n")
            planted.append(path)
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        for path in planted:
            self.assertEqual(path.read_bytes(), b"personal skill\n")

    def test_machine_delete_paths_are_removed(self):
        home = self.new_home("machine-delete-home")
        copied = self.copy_repo_without_local_machine_files("machine-delete-repo")
        machine_path = copied / "machines" / f"{TEST_MACHINE}.json"
        machine = read_json_test(machine_path)
        machine["delete"] = [".claude/agents/planted.md"]
        machine_path.write_text(json.dumps(machine), encoding="utf-8")
        planted = home / ".claude" / "agents" / "planted.md"
        planted.parent.mkdir(parents=True)
        planted.write_text("remove me\n", encoding="utf-8")
        result = self.run_install(home, copied)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(planted.exists())

    def test_read_only_skill_file_is_replaced(self):
        home = self.new_home("read-only-skill-home")
        copied = self.copy_repo_without_local_machine_files("read-only-skill-repo")
        skill = home / ".claude" / "skills" / "ship"
        stale = skill / ".git" / "objects" / "pack" / "stale.idx"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale\n", encoding="utf-8")
        os.chmod(stale, 0o444)

        result = self.run_install(home, copied)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue((skill / "SKILL.md").is_file())


class ProxyInstallerTests(unittest.TestCase):
    new_home = InstallerTests.new_home
    copy_repo_without_local_machine_files = InstallerTests.copy_repo_without_local_machine_files
    bundle_home = InstallerTests.bundle_home
    bundle_terms_file = InstallerTests.bundle_terms_file
    declare_bundle_domains = InstallerTests.declare_bundle_domains
    run_bundle = InstallerTests.run_bundle

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.new_home("home with spaces")
        self.repo = self.new_home("repo")
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.key = "sk-" + "cpa-TESTKEY"
        (self.home / "client.key").write_text(self.key + "\n", encoding="utf-8")
        self.machine = {
            "name": "probe", "os": "linux", "codex_model": "gpt-6-astra", "statusline": False,
            "proxy": {"host": "127.0.0.1", "key_file": "client.key"},
        }
        for relative, content in {
            "machines/probe.md": "# Probe\n", "claude/CLAUDE.md": "# Shared\n",
            "codex/AGENTS.md": "# Shared\n", "install-manifest.json": "{}",
        }.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def run_proxy_install(self, dry_run=False):
        (self.repo / "machines/probe.json").write_text(json.dumps(self.machine), encoding="utf-8")
        flag = "-" * 2
        args = [flag + "machine", "probe", flag + "home", str(self.home), flag + "repo", str(self.repo), flag + "no-tests"]
        if dry_run:
            args.append(flag + "dry-run")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.module.main(args)
        return result, output.getvalue()

    def written_paths(self):
        directory = self.module.proxy_path(self.module.proxy_bin_dir(self.machine), self.home)
        profiles = [self.home / ".claude-proxy/settings.json", self.home / ".codex-proxy/config.toml", self.home / ".codex-proxy/auth.json"]
        launchers = [directory / name for name in ("claude", "codex", "claude-proxy", "codex-proxy")]
        if self.machine["os"] == "windows":
            launchers += [path.with_suffix(".cmd") for path in launchers]
        return profiles, launchers

    def _probe_environment(self, curl_name, curl_body):
        probe_dir = self.home / "probe bin"
        probe_dir.mkdir()
        probe = probe_dir / curl_name
        probe.write_text(curl_body, encoding="utf-8", newline="\r\n" if curl_name.endswith(".cmd") else "\n")
        if not curl_name.endswith(".cmd"):
            probe.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = str(probe_dir) + os.pathsep + environment.get("PATH", "")
        environment["HARNESS_PROXY"] = ""
        for name in ("CODEX_HOME", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
            environment.pop(name, None)
        return environment

    def test_no_proxy_does_nothing(self):
        del self.machine["proxy"]
        result, output = self.run_proxy_install()
        self.assertEqual(result, 0, output)
        self.assertNotIn("proxy", output)
        self.assertFalse((self.home / ".claude-proxy").exists())
        self.assertFalse((self.home / ".codex-proxy").exists())
        self.assertFalse((self.home / ".claude/local/bin").exists())

    def test_proxy_profile_uses_configured_effort(self):
        import tomllib
        for effort in ("minimal", "low", "medium", "high", "xhigh"):
            with self.subTest(effort=effort):
                self.machine["codex_effort"] = effort
                result, output = self.run_proxy_install()
                self.assertEqual(result, 0, output)
                config = tomllib.loads((self.home / ".codex-proxy/config.toml").read_text(encoding="utf-8"))
                self.assertEqual(config["model_reasoning_effort"], effort)

    def test_proxy_effort_default_without_install(self):
        import tomllib
        installer = self.module.Installer(self.home, self.repo, False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.module.install_proxy(installer, self.machine, False, self.key)
        config = tomllib.loads((self.home / ".codex-proxy/config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["model_reasoning_effort"], "high")

    def test_proxy_null_effort_preserves_existing_value(self):
        import tomllib
        self.machine["codex_effort"] = None
        result, output = self.run_proxy_install()
        self.assertEqual(result, 0, output)
        path = self.home / ".codex-proxy/config.toml"
        self.assertNotIn("model_reasoning_effort", tomllib.loads(path.read_text(encoding="utf-8")))
        path.write_bytes(b'model_reasoning_effort = "high"\n' + path.read_bytes())
        result, output = self.run_proxy_install()
        self.assertEqual(result, 0, output)
        self.assertEqual(tomllib.loads(path.read_text(encoding="utf-8"))["model_reasoning_effort"], "high")

    def test_linux_profiles_and_launchers(self):
        import tomllib
        result, output = self.run_proxy_install()
        self.assertEqual(result, 0, output)
        profiles, launchers = self.written_paths()
        self.assertEqual(len(profiles + launchers), 7)
        settings = read_json_test(profiles[0])
        self.assertEqual(settings["env"], {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": self.key,
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        })
        self.assertNotIn("statusLine", settings)
        config = tomllib.loads(profiles[1].read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "gpt-6-astra")
        self.assertEqual(config["model_reasoning_effort"], "high")
        provider = config["model_providers"]["cliproxyapi"]
        self.assertEqual(provider["experimental_bearer_token"], self.key)
        self.assertFalse(provider["requires_openai_auth"])
        self.assertEqual(provider["base_url"], "http://127.0.0.1:8317/v1")
        self.assertEqual(provider["model_catalog_url"], "http://127.0.0.1:8317/v1/models")
        self.assertNotIn("projects", config)
        self.assertEqual(read_json_test(profiles[2]), {"OPENAI_API_KEY": self.key})
        self.assertTrue(profiles[2].read_bytes().endswith(b"\n"))
        for path in launchers:
            content = path.read_bytes()
            self.assertNotIn(self.key.encode(), content)
            self.assertNotIn(b"\r", content)
            self.assertIn(b"Written by install.py", content)
            self.assertIn(b"HARNESS_PROXY=off", content)
        if os.name != "nt":
            for path in profiles + launchers:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600 if path in profiles else 0o755)
        self.assertEqual(sum(str(path) in output for path in profiles + launchers), 7)

    def test_windows_launchers_and_line_endings(self):
        self.machine["os"] = "windows"
        result, output = self.run_proxy_install()
        self.assertEqual(result, 0, output)
        profiles, launchers = self.written_paths()
        self.assertEqual(len(profiles + launchers), 11)
        for path in profiles + launchers:
            content = path.read_bytes()
            if path in launchers:
                self.assertNotIn(self.key.encode(), content)
                if path.suffix == ".cmd":
                    self.assertIn(b"\r\n", content)
                    self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
                else:
                    self.assertNotIn(b"\r", content)
        codex = (self.home / ".claude/local/bin/codex.cmd").read_text()
        self.assertLess(codex.index("where $PATH:codex.cmd"), codex.index("where $PATH:codex 2"))
        self.assertIn("setlocal DisableDelayedExpansion", codex)
        self.assertIn("exit /b %errorlevel%", codex)
        self.assertIn("proxy launchers in", output)

    def test_darwin_defaults(self):
        self.machine["os"] = "darwin"
        del self.machine["codex_model"]
        native = self.home / ".local/bin/claude"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"native binary placeholder")
        self.assertEqual(self.run_proxy_install()[0], 0)
        self.assertTrue((self.home / ".claude/local/bin/claude").is_file())
        self.assertEqual(native.read_bytes(), b"native binary placeholder")
        self.assertIn('model = "gpt-6.1-sol"', (self.home / ".codex-proxy/config.toml").read_text())

    def test_preserve_settings_and_replace_environment(self):
        path = self.home / ".claude-proxy/settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"foreign": [1], "env": {"KEEP": "yes", "ANTHROPIC_AUTH_TOKEN": "old"}, "statusLine": None}))
        self.machine["statusline"] = True
        (self.repo / "claude/statusline-command.sh").write_text("#!/bin/sh\n")
        self.assertEqual(self.run_proxy_install()[0], 0)
        settings = read_json_test(path)
        self.assertEqual(settings["foreign"], [1])
        self.assertEqual(settings["env"]["KEEP"], "yes")
        self.assertEqual(settings["env"]["ANTHROPIC_AUTH_TOKEN"], self.key)
        self.assertIsNone(settings["statusLine"])
        self.assertTrue(list((self.home / ".claude/local").glob("backup-*")))

    def test_invalid_settings_are_replaced(self):
        path = self.home / ".claude-proxy/settings.json"
        path.parent.mkdir()
        for content in ("broken", "[]", '{"env": null}'):
            with self.subTest(content=content):
                path.write_text(content)
                self.assertEqual(self.run_proxy_install()[0], 0)
                self.assertEqual(read_json_test(path)["env"]["ANTHROPIC_AUTH_TOKEN"], self.key)

    def test_statusline_matches_normal_settings(self):
        self.machine["statusline"] = True
        (self.repo / "claude/statusline-command.sh").write_text("#!/bin/sh\n")
        self.assertEqual(self.run_proxy_install()[0], 0)
        self.assertEqual(read_json_test(self.home / ".claude-proxy/settings.json")["statusLine"], read_json_test(self.home / ".claude/settings.json")["statusLine"])

    def test_preserves_existing_projects_tail_unchanged(self):
        path = self.home / ".codex-proxy/config.toml"
        path.parent.mkdir()
        tail = b'[projects."/old"] # Keep this entry\r\ntrust_level = "trusted"\r\n\r\n[other]\r\nvalue = 1\r\n\r\n'
        path.write_bytes(b'model = "old"\n' + tail)
        self.machine["codex_model"] = "test-model"
        self.assertEqual(self.run_proxy_install()[0], 0)
        first = path.read_bytes()
        self.assertEqual(self.run_proxy_install()[0], 0)
        self.assertEqual(path.read_bytes(), first)
        content = first.decode()
        self.assertTrue(first.endswith(tail))
        self.assertIn('model = "test-model"', content)
        self.assertEqual(content.count('[projects."'), 1)

    def test_key_rotation_updates_auth(self):
        self.assertEqual(self.run_proxy_install()[0], 0)
        (self.home / "client.key").write_text("replacement-placeholder\n")
        self.assertEqual(self.run_proxy_install()[0], 0)
        for path in self.written_paths()[0]:
            self.assertIn("replacement-placeholder", path.read_text())
            self.assertNotIn(self.key, path.read_text())

    def test_missing_key_refuses_before_writes(self):
        self.machine["proxy"]["key_file"] = "missing.key"
        result, output = self.run_proxy_install()
        self.assertEqual(result, 2)
        self.assertIn(f"install refused: proxy key file not found: {self.home / 'missing.key'} (copy the device's client key there by hand first)", output)
        self.assertFalse((self.home / ".claude").exists())

    def test_missing_key_dry_run_lists_every_path(self):
        self.machine["proxy"]["key_file"] = "missing.key"
        result, output = self.run_proxy_install(dry_run=True)
        self.assertEqual(result, 0, output)
        self.assertEqual(output.count(f"would write (key file missing: {self.home / 'missing.key'})"), 7)
        for path in sum(self.written_paths(), []):
            self.assertIn(str(path), output)
            self.assertFalse(path.exists())

    def test_dry_run_with_key_writes_nothing(self):
        result, output = self.run_proxy_install(dry_run=True)
        self.assertEqual(result, 0, output)
        for path in sum(self.written_paths(), []):
            self.assertIn(str(path), output)
            self.assertFalse(path.exists())

    def test_proxy_validation_types_and_required_keys(self):
        valid = self.machine["proxy"].copy()
        cases = [(None, "proxy must be an object"), ([], "proxy must be an object")]
        for key in ("host", "key_file", "claude_command", "codex_command", "bin_dir"):
            for value in (None, 1, [], "", " "):
                cases.append(({**valid, key: value}, f"proxy.{key} must be a non empty string"))
        for key in ("host", "key_file"):
            cases.append(({field: value for field, value in valid.items() if field != key}, f"proxy.{key} must be a non empty string"))
        for proxy, message in cases:
            with self.subTest(proxy=proxy):
                self.machine["proxy"] = proxy
                result, output = self.run_proxy_install()
                self.assertEqual(result, 2, output)
                self.assertIn("install refused:", output)
                self.assertIn(message, output)

    def test_proxy_validation_port_scheme_unknown_os(self):
        valid = self.machine["proxy"].copy()
        for key, values, message in (
            ("port", (0, 65536, True, 1.5, "8317", None), "port must be an integer from 1 to 65535"),
            ("scheme", ("ftp", "", 1, [], None), "scheme must be http or https"),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.machine["proxy"] = {**valid, key: value}
                    result, output = self.run_proxy_install()
                    self.assertEqual(result, 2, output)
                    self.assertIn(message, output)
        self.machine["proxy"] = {**valid, "z": 1, "a": 2}
        self.assertIn("unknown keys: a, z", self.run_proxy_install()[1])
        self.machine["proxy"] = valid
        self.machine["os"] = "other-os"
        self.assertIn("got 'other-os'", self.run_proxy_install()[1])

    def test_trusted_is_an_unknown_key(self):
        self.machine["proxy"]["trusted"] = ["/tmp/work"]
        result, output = self.run_proxy_install()
        self.assertEqual(result, 2, output)
        self.assertIn("proxy has unknown keys: trusted", output)
        self.assertFalse((self.home / ".claude").exists())

    def test_foreign_launcher_refuses_whole_install(self):
        self.machine["os"] = "windows"
        path = self.home / ".claude/local/bin/codex-proxy.cmd"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"foreign binary placeholder")
        before = self.module.tree_snapshot(self.home)
        result, output = self.run_proxy_install()
        self.assertEqual(result, 2, output)
        self.assertIn(f"install refused: {path} exists and was not written by install.py (set proxy.bin_dir to another folder or move that file)", output)
        self.assertEqual(self.module.tree_snapshot(self.home), before)
        self.assertNotIn("ACTION", output)

    def test_foreign_launcher_dry_run_reports_and_continues(self):
        path = self.home / ".claude/local/bin/claude"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"foreign binary placeholder")
        before = self.module.tree_snapshot(self.home)
        for key_file in ("client.key", "missing.key"):
            with self.subTest(key_file=key_file):
                self.machine["proxy"]["key_file"] = key_file
                result, output = self.run_proxy_install(dry_run=True)
                self.assertEqual(result, 0, output)
                self.assertIn(f"ACTION  {path}  (would refuse (existing file not written by install.py))", output)
                self.assertEqual(output.count("would refuse"), 1)
                self.assertIn("install complete", output)
                for destination in sum(self.written_paths(), []):
                    self.assertIn(str(destination), output)
                self.assertEqual(self.module.tree_snapshot(self.home), before)

    def check_symlink_collision(self, dangling):
        self.machine["proxy"]["bin_dir"] = ".local/bin"
        path = self.home / ".local/bin/claude"
        path.parent.mkdir(parents=True)
        target = self.home / "native-claude"
        if not dangling:
            target.write_bytes(b"native binary placeholder")
        try:
            path.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        for dry_run in (False, True):
            result, output = self.run_proxy_install(dry_run=dry_run)
            self.assertEqual(result, 0 if dry_run else 2, output)
            self.assertIn("would refuse" if dry_run else "install refused:", output)
            self.assertIn(str(path), output)
            self.assertTrue(path.is_symlink())
            self.assertEqual(Path(os.readlink(path)), target)
            self.assertFalse((self.home / ".claude/local/machine-name").exists())
            if dangling:
                self.assertFalse(target.exists())
            else:
                self.assertEqual(target.read_bytes(), b"native binary placeholder")

    def test_native_symlink_is_not_overwritten(self):
        self.check_symlink_collision(dangling=False)

    def test_dangling_symlink_is_not_overwritten(self):
        self.check_symlink_collision(dangling=True)

    def test_managed_launcher_can_be_updated(self):
        path = self.home / ".claude/local/bin/claude"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"# Written by install.py from the machine's proxy block\nold launcher\n")
        result, output = self.run_proxy_install()
        self.assertEqual(result, 0, output)
        self.assertIn(b"claude via the proxy pool", path.read_bytes())
        self.assertNotIn(b"old launcher", path.read_bytes())

    def test_path_note_only_when_launcher_folder_is_absent(self):
        original = os.environ.get("PATH")
        directory = self.home / ".claude/local/bin"
        try:
            for system in ("linux", "darwin", "windows"):
                self.machine["os"] = system
                for present in (False, True):
                    with self.subTest(system=system, present=present):
                        entry = str(directory if present else self.home / "other")
                        os.environ["PATH"] = entry.swapcase() if system == "windows" else entry
                        result, output = self.run_proxy_install(dry_run=True)
                        self.assertEqual(result, 0, output)
                        self.assertEqual("proxy launchers in" in output, not present)
        finally:
            if original is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = original

    def test_path_note_checks_precedence_and_expands_entries(self):
        directory = self.home / ".claude/local/bin"
        for system, suffix in (("linux", ""), ("darwin", ""), ("windows", ".exe"), ("windows", ".cmd")):
            real_dir = self.home / (system + (suffix or "-plain"))
            real_dir.mkdir()
            binary = real_dir / ("codex" + suffix)
            binary.write_bytes(b"binary placeholder")
            if os.name != "nt":
                os.chmod(binary, 0o755)
            self.machine["os"] = system
            for entries, warning in (
                (["$PROXY_TEST_REAL", "~/.claude/local/bin"], True),
                (["~/.claude/local/bin", "$PROXY_TEST_REAL"], False),
                (["$PROXY_TEST_REAL"], True),
                (["$PROXY_TEST_REAL", "~/.claude/local/bin", "$PROXY_TEST_REAL"], True),
            ):
                with self.subTest(system=system, suffix=suffix, entries=entries), mock.patch.dict(os.environ, {
                    "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "PROXY_TEST_REAL": str(real_dir), "PATH": os.pathsep.join(entries),
                }):
                    result, output = self.run_proxy_install(dry_run=True)
                    self.assertEqual(result, 0, output)
                    message = f"proxy launchers in {directory}: this folder must come before {real_dir} on PATH, or the bare claude and codex skip the pool"
                    self.assertEqual(message in output, warning)
                    self.assertEqual("proxy launchers in" in output, warning)

    def test_path_note_skips_managed_launchers(self):
        earlier = self.home / "earlier"
        earlier.mkdir()
        (earlier / "codex").write_bytes(b"# Written by install.py from the machine's proxy block\n")
        if os.name != "nt":
            os.chmod(earlier / "codex", 0o755)
        directory = self.home / ".claude/local/bin"
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join((str(earlier), str(directory)))}):
            result, output = self.run_proxy_install(dry_run=True)
        self.assertEqual(result, 0, output)
        self.assertNotIn("proxy launchers in", output)

    def test_cmd_lookup_restores_path_and_preserves_arguments(self):
        self.machine["os"] = "windows"
        self.assertEqual(self.run_proxy_install()[0], 0)
        directory = self.home / ".claude/local/bin"
        for name in ("claude", "codex"):
            content = (directory / (name + ".cmd")).read_text()
            lookups = [content.index(f"where $PATH:{name}{suffix} 2") for suffix in (".exe", ".cmd", "")]
            self.assertEqual(lookups, sorted(lookups))
            self.assertEqual(content.count('do if not defined real set "real=%%R"'), 3)
            saved = content.index('set "proxy_orig_path=%PATH%"')
            stripped = content.index('set "PATH=%proxy_path%"')
            restored = content.index('set "PATH=%proxy_orig_path%"')
            self.assertLess(saved, stripped)
            self.assertLess(stripped, lookups[0])
            self.assertLess(lookups[-1], restored)
            self.assertLess(restored, content.index("if defined real goto resolved"))
            self.assertLess(restored, content.index('"%real%" '))
            self.assertNotIn('call "', content)
            self.assertIn('"%real%" %*\nexit /b %errorlevel%', content)
            alias = (directory / (name + "-proxy.cmd")).read_text()
            self.assertNotIn('call "', alias)
            self.assertTrue(alias.endswith(f'"%~dp0{name}.cmd" %*\n'))

    def test_codex_launchers_use_environment_key(self):
        self.machine["os"] = "windows"
        self.assertEqual(self.run_proxy_install()[0], 0)
        directory = self.home / ".claude/local/bin"
        for suffix in ("", ".cmd"):
            content = (directory / ("codex" + suffix)).read_text()
            self.assertNotIn(self.key, content)
            self.assertIn("model_providers.cliproxyapi.env_key=", content)
            self.assertIn("model_providers.cliproxyapi.requires_openai_auth=false", content)
            self.assertNotIn("model_providers.cliproxyapi.experimental_bearer_token=", content)
            self.assertIn("experimental_bearer_token", content)
            self.assertEqual(content.count("codex_login=off"), 1)
            if suffix:
                self.assertIn('endlocal & set "CLIPROXY_API_KEY=%tok%" & (if not defined RUST_LOG set "RUST_LOG=error,codex_login=off") & "%real%" %sub% -c ', content)
                marker, token, own_args = '"%real%"', "%tok%", "%*"
                env_key = r'env_key=\"CLIPROXY_API_KEY\"'
            else:
                self.assertIn('RUST_LOG="${RUST_LOG:-error,codex_login=off}" CLIPROXY_API_KEY="$tok" exec "$real"', content)
                marker, token, own_args = 'exec "$real"', "$tok", '"$@"'
                env_key = 'env_key="CLIPROXY_API_KEY"'
            pool_invocations = 0
            for line in content.splitlines():
                if marker not in line:
                    continue
                invocation = line.split(marker, 1)[1].strip()
                self.assertNotIn(token, invocation)
                if invocation == own_args:
                    self.assertNotIn("env_key", line)
                    self.assertNotIn(token, line)
                    self.assertNotIn("RUST_LOG", line)
                else:
                    self.assertIn(env_key, invocation)
                    pool_invocations += 1
            self.assertEqual(pool_invocations, 1)

    def test_codex_launchers_place_overrides_after_subcommands(self):
        self.machine["os"] = "windows"
        self.assertEqual(self.run_proxy_install()[0], 0)
        directory = self.home / ".claude/local/bin"
        shell = (directory / "codex").read_text()
        self.assertIn('CLIPROXY_API_KEY="$tok" exec "$real" ${sub:+"$sub"} ${nested:+"$nested"} -c ', shell)
        self.assertIn('""|-*|help|', shell)
        batch = (directory / "codex.cmd").read_text()
        captures = [line for line in batch.splitlines() if line.startswith("for /f ") and '"!args!"' in line]
        self.assertEqual(len(captures), 2)
        for capture in captures:
            self.assertIn("eol=", capture)
            self.assertRegex(capture, 'delims=[ \\t]*\\t[ \\t]*"')
        self.assertRegex(batch, r'if /i "!sub!"=="help" goto proxy_root\w*')
        self.assertRegex(batch, r'"%real%" %sub% -c [^\r\n]+ %rest%')
        self.assertEqual(batch.count('endlocal & set "CLIPROXY_API_KEY=%tok%"'), 1)
        for name in ("claude", "codex"):
            self.assertIn('call curl -sf -m 2', (directory / (name + ".cmd")).read_text())
        for content, marker, token in ((shell, 'exec "$real"', "$tok"), (batch, '"%real%"', "%tok%")):
            self.assertNotIn(self.key, content)
            for line in content.splitlines():
                if marker in line:
                    self.assertNotIn(token, line.split(marker, 1)[1])

    @unittest.skipUnless(
        shutil.which("sh") and not any(part.lower() in ("system32", "windowsapps") for part in Path(shutil.which("sh")).parts),
        "requires a native POSIX sh on PATH, not a WSL launcher",
    )
    def test_codex_shell_launcher_argument_placement(self):
        self.machine["os"] = "linux"
        binary = self.home / "real codex"
        self.machine["proxy"]["codex_command"] = str(binary)
        self.assertEqual(self.run_proxy_install()[0], 0)
        binary.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n', encoding="utf-8", newline="\n")
        binary.chmod(0o755)
        environment = self._probe_environment("curl", "#!/bin/sh\nexit 0\n")
        overrides = []
        for value in self.module.codex_proxy_overrides("http://127.0.0.1:8317"):
            overrides.extend(["-c", value])
        for arguments, expected in (
            (["exec", "-", "-m", "x"], ["exec"] + overrides + ["-", "-m", "x"]),
            (["exec", "resume", "abc"], ["exec", "resume"] + overrides + ["abc"]),
            (["-m", "x"], overrides + ["-m", "x"]),
            ([], overrides),
            (["resume", "abc"], ["resume"] + overrides + ["abc"]),
            (["exec", "two words", ""], ["exec"] + overrides + ["two words", ""]),
            (["fix a&b"], overrides + ["fix a&b"]),
            (["two words prompt"], overrides + ["two words prompt"]),
            (['say "hi"'], overrides + ['say "hi"']),
            (["two\twords"], overrides + ["two\twords"]),
            (["help"], overrides + ["help"]),
            (["help", "exec"], overrides + ["help", "exec"]),
            (["exec", "review", "x"], ["exec", "review"] + overrides + ["x"]),
            (["exec", "fork", "abc"], ["exec", "fork"] + overrides + ["abc"]),
            (["-m", "x", "exec"], overrides + ["-m", "x", "exec"]),
            (["exec", "--json", "resume", "abc"], ["exec"] + overrides + ["--json", "resume", "abc"]),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [shutil.which("sh"), (self.home / ".claude/local/bin/codex").as_posix(), *arguments],
                    env=environment, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("codex via the proxy pool", result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
    def test_codex_cmd_launcher_argument_placement(self):
        self.machine["os"] = "windows"
        binary = self.home / "real codex.cmd"
        self.machine["proxy"]["codex_command"] = str(binary)
        self.assertEqual(self.run_proxy_install()[0], 0)
        native_probe = self.home / "native arguments.py"
        native_output = self.home / "native arguments.json"
        native_probe.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        binary.write_text(f'''@echo off
setlocal DisableDelayedExpansion
"{sys.executable}" "{native_probe}" %* > "{native_output}"
:loop
if [%1]==[] goto done
echo(%1
shift
goto loop
:done
echo %CLIPROXY_API_KEY%
''', encoding="utf-8", newline="\r\n")
        environment = self._probe_environment("curl.cmd", "@exit /b 0\n")
        overrides = []
        native_overrides = []
        for value in self.module.codex_proxy_overrides("http://127.0.0.1:8317"):
            overrides.extend(["-c", '"' + value.replace('"', r'\"') + '"'])
            native_overrides.extend(["-c", value])
        launcher = self.home / ".claude/local/bin/codex.cmd"
        for arguments, prefix, remainder, native_remainder in (
            ("exec - -m x", ["exec"], ["-", "-m", "x"], ["-", "-m", "x"]),
            ("exec resume abc", ["exec", "resume"], ["abc"], ["abc"]),
            ("-m x", [], ["-m", "x"], ["-m", "x"]),
            ("", [], [], []),
            ('exec - -c model_reasoning_effort="high" --sandbox workspace-write', ["exec"],
             ["-", "-c", "model_reasoning_effort", '"high"', "--sandbox", "workspace-write"],
             ["-", "-c", "model_reasoning_effort=high", "--sandbox", "workspace-write"]),
            ("exec resume", ["exec", "resume"], [], []),
            ("app-server", ["app-server"], [], []),
            ('exec "a&b"', ["exec"], ['"a&b"'], ["a&b"]),
            (r'exec "C:\Program Files (x86)\x"', ["exec"], [r'"C:\Program Files (x86)\x"'], [r'C:\Program Files (x86)\x']),
            ("exec", ["exec"], [], []),
            ('exec resume "a&b"', ["exec", "resume"], ['"a&b"'], ["a&b"]),
            ('exec "a|b"', ["exec"], ['"a|b"'], ["a|b"]),
            ('exec "a(b)c"', ["exec"], ['"a(b)c"'], ["a(b)c"]),
            ('exec "a=b,c;d"', ["exec"], ['"a=b,c;d"'], ["a=b,c;d"]),
            ('exec "a""b"', ["exec"], ['"a""b"'], ['a"b']),
            ('exec "a!PATH!b"', ["exec"], ['"a!PATH!b"'], ["a!PATH!b"]),
            ('exec resume "a!PATH!b"', ["exec", "resume"], ['"a!PATH!b"'], ["a!PATH!b"]),
            ('"fix a&b"', [], ['"fix a&b"'], ["fix a&b"]),
            ('"two words prompt"', [], ['"two words prompt"'], ["two words prompt"]),
            ('"a(b)c"', [], ['"a(b)c"'], ["a(b)c"]),
            ('"a!PATH!b"', [], ['"a!PATH!b"'], ["a!PATH!b"]),
            ('"exec"', [], ['"exec"'], ["exec"]),
            ('fi"x a&b"', [], ['fi"x a&b"'], ["fix a&b"]),
            ("help", [], ["help"], ["help"]),
            ("help exec", [], ["help", "exec"], ["help", "exec"]),
            ("exec\t-", ["exec"], ["-"], ["-"]),
            ('exec "a\tb"', ["exec"], ['"a\tb"'], ["a\tb"]),
            ("exec ;x y", ["exec"], ["x", "y"], [";x", "y"]),
            (";x y", ["x"], ["y"], ["y"]),
            ("exec review x", ["exec", "review"], ["x"], ["x"]),
            ("exec fork abc", ["exec", "fork"], ["abc"], ["abc"]),
            ("-m x exec", [], ["-m", "x", "exec"], ["-m", "x", "exec"]),
            ("exec --json resume abc", ["exec"], ["--json", "resume", "abc"], ["--json", "resume", "abc"]),
            (r'exec "say \"hi there\""', ["exec"], [r'"say \"hi', r'there\""'], ['say "hi there"']),
            ('exec fi"x y"', ["exec"], ['fi"x y"'], ["fix y"]),
            ('exec "a  b" last', ["exec"], ['"a  b"', "last"], ["a  b", "last"]),
            ("exec,resume abc", ["exec", "resume"], ["abc"], ["abc"]),
            ("exec;resume abc", ["exec", "resume"], ["abc"], ["abc"]),
            ("exec=resume abc", ["exec", "resume"], ["abc"], ["abc"]),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    f'cmd.exe /d /c ""{launcher}" {arguments}"',
                    env=environment, capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("codex via the proxy pool", result.stderr)
                self.assertEqual(result.stdout.splitlines().count(self.key), 1)
                self.assertEqual(result.stdout.splitlines(), prefix + overrides + remainder + [self.key])
                native_prefix = {
                    ";x y": [";x"], "exec,resume abc": ["exec,resume"],
                    "exec;resume abc": ["exec;resume"], "exec=resume abc": ["exec=resume"],
                }.get(arguments, prefix)
                self.assertEqual(json.loads(native_output.read_text()), native_prefix + native_overrides + native_remainder)

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe")
    def test_claude_cmd_launcher_returns_from_probe_shim(self):
        self.machine["os"] = "windows"
        binary = self.home / "real claude.cmd"
        self.machine["proxy"]["claude_command"] = str(binary)
        self.assertEqual(self.run_proxy_install()[0], 0)
        binary.write_text('@echo off\necho %ANTHROPIC_BASE_URL%\necho %ANTHROPIC_AUTH_TOKEN%\n', encoding="utf-8", newline="\r\n")
        environment = self._probe_environment("curl.cmd", "@exit /b 0\n")
        launcher = self.home / ".claude/local/bin/claude.cmd"
        result = subprocess.run(
            f'cmd.exe /d /c ""{launcher}""',
            env=environment, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude via the proxy pool", result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["http://127.0.0.1:8317", self.key])

    def test_key_rejects_characters_outside_plain_key_alphabet(self):
        path = self.home / "client.key"
        for character in ("&", "|", "<", ">", "%", "^", '"', " ", "\t", "\n", "\x00", "\u00e9"):
            with self.subTest(character=character):
                path.write_text(self.key + character + "bad\n", encoding="utf-8")
                result, output = self.run_proxy_install()
                self.assertEqual(result, 2, output)
                self.assertIn(f"install refused: proxy key file holds characters outside letters, digits, dash and underscore: {path}", output)
                self.assertFalse((self.home / ".claude").exists())
        path.write_text("abc_123-XYZ\n", encoding="utf-8")
        self.assertEqual(self.run_proxy_install()[0], 0)

    def test_commands_expand_home_environment_and_relative_paths(self):
        self.machine["os"] = "windows"
        self.machine["proxy"].update({
            "claude_command": "~/.local/share/claude/claude",
            "codex_command": "$PROXY_TEST_COMMANDS/codex.cmd",
        })
        with mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "PROXY_TEST_COMMANDS": "real commands",
        }):
            self.assertEqual(self.run_proxy_install()[0], 0)
        for name, expected in (
            ("claude", self.home / ".local/share/claude/claude"),
            ("codex", self.home / "real commands/codex.cmd"),
        ):
            directory = self.home / ".claude/local/bin"
            self.assertIn(expected.as_posix(), (directory / name).read_text())
            self.assertIn(f'set "real={expected}"', (directory / (name + ".cmd")).read_text())

    def test_custom_paths_commands_and_https(self):
        self.machine["proxy"].update({
            "bin_dir": "custom bin", "claude_command": "/opt/real cli/claude",
            "codex_command": "/opt/real cli/codex", "scheme": "https", "port": 443,
            "key_file": str(self.home / "client.key"),
        })
        self.assertEqual(self.run_proxy_install()[0], 0)
        self.assertIn("https://127.0.0.1:443", (self.home / ".claude-proxy/settings.json").read_text())
        for name in ("claude", "codex"):
            content = (self.home / "custom bin" / name).read_text()
            expected = self.module.proxy_path(f"/opt/real cli/{name}", self.home).as_posix()
            self.assertIn(f"real='{expected}'", content)
        self.assertEqual(self.module.proxy_path("~/key", self.home), Path("~/key").expanduser().resolve())

    def test_launchers_have_bypass_probe_overrides_and_force_aliases(self):
        self.assertEqual(self.run_proxy_install()[0], 0)
        directory = self.home / ".claude/local/bin"
        for name in ("claude", "codex"):
            content = (directory / name).read_text()
            self.assertIn("curl -sf -m 2", content)
            self.assertIn("Authorization: Bearer $tok", content)
            self.assertIn("http://127.0.0.1:8317/v1/models", content)
            self.assertIn('if [ "$HARNESS_PROXY" = "off" ]', content)
            self.assertIn('echo "proxy unreachable" >&2', content)
            self.assertIn(f"proxy down, {name} on this device's own login", content)
            self.assertIn(f"{name}: real binary not found on PATH", content)
            self.assertIn('HARNESS_PROXY=force exec "$(dirname "$0")/' + name, (directory / (name + "-proxy")).read_text())
        claude = (directory / "claude").read_text()
        codex = (directory / "codex").read_text()
        self.assertIn("ANTHROPIC_BASE_URL+x", claude)
        self.assertNotIn("CLAUDE_CONFIG_DIR=", claude)
        self.assertIn("CODEX_HOME+x", codex)
        self.assertIn("OPENAI_BASE_URL+x", codex)
        self.assertNotIn("CODEX_HOME=", codex)
        self.assertIn('RUST_LOG="${RUST_LOG:-error,codex_login=off}"', codex)
        self.assertEqual(codex.count("codex_login=off"), 1)
        for key in ("name", "base_url", "model_catalog_url", "wire_api", "requires_openai_auth", "supports_websockets", "env_key"):
            self.assertIn("model_providers.cliproxyapi." + key, codex)

    def test_bundle_instructions_only_with_proxy(self):
        plain = self.module.bundle_install_text("probe", "date", "commit", "gate")
        self.assertNotIn("client key", plain)
        self.assertEqual(re.findall(r"^(\d+)\. (\w+)", plain, re.MULTILINE), [
            ("1", "On"), ("2", "Dry"), ("3", "Real"), ("4", "Restart"),
        ])
        self.machine["proxy"]["key_file"] = "~/client.key"
        text = self.module.bundle_install_text("probe", "date", "commit", "gate", machine=self.machine)
        self.assertIn("~/client.key", text)
        self.assertIn("Before running install.py", text)
        self.assertIn(".claude/local/bin", text)
        self.assertIn("must precede the real binaries on PATH", text)
        self.assertEqual(re.findall(r"^(\d+)\. (\w+)", text, re.MULTILINE), [
            ("1", "On"), ("2", "Before"), ("3", "Dry"), ("4", "Real"), ("5", "Restart"),
        ])

    @unittest.skipUnless(BUNDLE_BUILD_TESTS, "bundle gate requires the source repository")
    def test_bundle_merges_proxy_and_gate_refuses_key(self):
        copied = self.copy_repo_without_local_machine_files("bundle-repo")
        local = copied / "machines" / f"{TEST_MACHINE}.local.json"
        local.write_text(json.dumps({"os": "linux", "proxy": {"host": "127.0.0.1", "key_file": "~/client.key"}}))
        output = self.base / "bundle.zip"
        result = self.run_bundle(copied, output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with zipfile.ZipFile(output) as bundle:
            self.assertIn("~/client.key", bundle.read("INSTALL.md").decode())
            self.assertFalse(any(name.endswith("client.key") for name in bundle.namelist()))
        (copied / "claude/CLAUDE.md").write_text(self.key)
        result = self.run_bundle(copied, self.base / "refused.zip")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("GATE claude/CLAUDE.md", result.stdout)
        self.assertFalse((self.base / "refused.zip").exists())


class CommitMsgGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def run_guard(self, message, cwd=None):
        path = self.home / "message.txt"
        path.write_text(message, encoding="utf-8")
        environment = os.environ.copy()
        environment["CLAUDE_HOOKS_HOME"] = str(self.home)
        return subprocess.run(
            [
                sys.executable,
                str(HOOK_DIR / "commit_msg_guard.py"),
                "--check-only",
                str(path),
            ],
            cwd=cwd or REPO,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )

    def write_machine(self, value):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True, exist_ok=True)
        (local / "machine.json").write_text(json.dumps(value), encoding="utf-8")

    def git_repo(self, expected):
        repo = self.home / "repo"
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "a@b.c"], cwd=repo, check=True
        )
        self.write_machine({"identities": {str(repo): expected}})
        return repo

    def test_attribution_trailer_is_rejected(self):
        result = self.run_guard("Subject\nCo-Authored-By: X <x@y>\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("line 2", result.stderr)

    def test_clean_message_is_accepted(self):
        result = self.run_guard("Clean subject\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_robot_generated_line_is_rejected(self):
        result = self.run_guard("Subject\n\N{ROBOT FACE} Generated by a tool\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("line 2", result.stderr)

    def test_extra_machine_regex_is_rejected(self):
        self.write_machine({"commit_msg_deny_regex": [r"ticket-[0-9]+"]})
        result = self.run_guard("Fix ticket-123\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ticket-[0-9]+", result.stderr)

    def test_identity_mismatch_is_rejected(self):
        repo = self.git_repo("x@y.z")
        result = self.run_guard("Clean subject\n", cwd=repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("expects x@y.z", result.stderr)

    def test_matching_identity_is_accepted(self):
        repo = self.git_repo("a@b.c")
        result = self.run_guard("Clean subject\n", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_todo_identity_is_skipped(self):
        repo = self.git_repo("TODO-client-email")
        result = self.run_guard("Clean subject\n", cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped", result.stderr)

    def test_missing_machine_is_accepted(self):
        result = self.run_guard("Clean subject\n")
        self.assertEqual(result.returncode, 0, result.stderr)


class GhCredentialHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "argv.jsonl"
        self.script = self.bin / "fake_gh.py"
        self.script.write_text(
            "import json, os, sys\n"
            "with open(os.environ['GH_TEST_LOG'], 'a') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:4] == ['auth', 'token', '--user'] and sys.argv[4] == 'known' and sys.argv[-1] == 'github.com':\n"
            "    print('TEST_TOKEN_PLACEHOLDER')\n"
            "else:\n"
            "    print('TEST_ERROR_PLACEHOLDER', file=sys.stderr)\n"
            "    sys.exit(1)\n",
            encoding="utf-8",
        )
        launcher = self.bin / ("gh.bat" if os.name == "nt" else "gh")
        if os.name == "nt":
            launcher.write_text(f'@echo off\n"{sys.executable}" "{self.script}" %*\n', encoding="utf-8")
        else:
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.script}" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(self.bin), GH_TEST_LOG=str(self.log))

    def invoke(self, operation="get", username="known", host="github.com"):
        request = f"protocol=https\nhost={host}\n"
        if username is not None:
            request += f"username={username}\n"
        return subprocess.run(
            [sys.executable, "-I", str(HOOK_DIR / "gh_credential_helper.py"), operation],
            input=request + "\n", env=self.environment, capture_output=True, text=True, timeout=10,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_git_credential_fill_uses_installed_helper(self):
        spec = importlib.util.spec_from_file_location("install", REPO / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        home = self.base / "home with spaces"
        home.mkdir()
        installer = module.Installer(home, REPO, False)
        installer.copy_file(HOOK_DIR / "gh_credential_helper.py", home / ".claude/hooks/gh_credential_helper.py")
        installer.write_bytes(home / ".gitconfig", b'[credential "https://github.com"]\n\thelper = !echo password=WRONG_TEST_PLACEHOLDER\n')
        system_config = home / "system.gitconfig"
        installer.write_bytes(system_config, b"[credential]\n\thelper = !echo password=WRONG_TEST_PLACEHOLDER\n")
        module.install_gh_credentials(installer, {"gh_account": "known"})
        environment = {key: value for key, value in self.environment.items() if not key.startswith("GIT_CONFIG")}
        environment.update(HOME=str(home), GIT_CONFIG_SYSTEM=str(system_config), GIT_CONFIG_GLOBAL=str(home / ".gitconfig"), GIT_TERMINAL_PROMPT="0")
        environment["PATH"] += os.pathsep + os.environ.get("PATH", "")
        result = subprocess.run([shutil.which("git"), "credential", "fill"], cwd=home, input="protocol=https\nhost=github.com\n\n", env=environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("username=known\npassword=TEST_TOKEN_PLACEHOLDER\n", result.stdout)
        self.assertEqual(self.calls(), [["auth", "token", "--user", "known", "--hostname", "github.com"]])

    def test_username_returns_credentials(self):
        for host in ("github.com", "gist.github.com"):
            with self.subTest(host=host):
                result = self.invoke(host=host)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(result.stdout, "username=known\npassword=TEST_TOKEN_PLACEHOLDER\n\n")
                self.assertEqual(self.calls()[-1], ["auth", "token", "--user", "known", "--hostname", "github.com"])

    def test_unknown_login_is_silent(self):
        result = self.invoke(username="unknown")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_gh_failure_is_silent(self):
        self.script.write_text("import sys\nprint('TEST_SECRET_PLACEHOLDER')\nprint('TEST_SECRET_PLACEHOLDER', file=sys.stderr)\nsys.exit(1)\n", encoding="utf-8")
        result = self.invoke()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_no_username_is_silent(self):
        result = self.invoke(username=None)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_no_gh_is_silent(self):
        self.environment["PATH"] = ""
        result = self.invoke()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_store_and_erase_are_silent(self):
        for operation in ("store", "erase"):
            result = self.invoke(operation)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_other_host_is_silent(self):
        result = self.invoke(host="example.invalid")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_timeout_is_silent(self):
        spec = importlib.util.spec_from_file_location("gh_credential_helper", HOOK_DIR / "gh_credential_helper.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.sys, "argv", ["helper", "get"]), mock.patch.object(module.sys, "stdin", io.StringIO("host=github.com\nusername=known\n\n")), mock.patch.object(module.shutil, "which", return_value="gh"), mock.patch.object(module.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 10)) as run, contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(module.main(), 0)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
            self.assertEqual(run.call_args.kwargs["timeout"], 10)


class GitHooksInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "repo"
        subprocess.run(["git", "init", str(self.repo)], capture_output=True, check=True)

    def tearDown(self):
        self.temporary.cleanup()

    def run_install(self, path_value="", gitleaks=None):
        environment = os.environ.copy()
        environment["PATH"] = path_value
        command = [
            sys.executable,
            str(REPO / "install.py"),
            "--git-hooks",
            str(self.repo),
            "--home",
            str(self.home),
        ]
        if gitleaks is not None:
            command.extend(["--gitleaks", str(gitleaks)])
        return subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )

    def make_gitleaks(self):
        binary = self.base / ("gitleaks.bat" if os.name == "nt" else "gitleaks")
        if os.name == "nt":
            binary.write_text(
                '@echo off\nif "%1"=="version" echo v8.20.0\n', encoding="utf-8"
            )
        else:
            binary.write_text(
                '#!/bin/sh\n[ "$1" = version ] && echo v8.20.0\n', encoding="utf-8"
            )
            os.chmod(binary, 0o755)
        return binary

    def test_commit_message_hook_is_written(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = self.repo / ".git" / "hooks" / "commit-msg"
        lines = hook.read_text(encoding="utf-8").splitlines()
        self.assertIn("commit_msg_guard.py", lines[1])

    def test_new_gitleaks_command_is_written(self):
        binary = self.make_gitleaks()
        result = self.run_install(str(binary.parent))
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        expected = str(binary.resolve()).replace("\\", "/")
        self.assertIn(
            f'exec "{expected}" git --pre-commit', hook.read_text(encoding="utf-8")
        )

        hook.unlink()
        override = self.run_install(gitleaks=binary)
        self.assertEqual(override.returncode, 0, override.stderr)
        self.assertIn(
            f'exec "{expected}" git --pre-commit', hook.read_text(encoding="utf-8")
        )

    def test_absent_gitleaks_skips_pre_commit(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / ".git" / "hooks" / "pre-commit").exists())
        self.assertIn("gitleaks not on PATH", result.stdout)

    def test_second_run_does_not_add_backup(self):
        hook = self.repo / ".git" / "hooks" / "commit-msg"
        hook.write_text("old hook\n", encoding="utf-8")
        first = self.run_install()
        second = self.run_install()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(list(hook.parent.glob("commit-msg.backup-*"))), 1)


class CheckWritingTests(unittest.TestCase):
    def setUp(self):
        if not (REPO / "checkers" / "check_writing.py").is_file():
            self.skipTest("writing checker is absent from the repository")
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def run_checker(self, path):
        return subprocess.run(
            [sys.executable, str(REPO / "checkers" / "check_writing.py"), str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )

    def test_four_writing_findings(self):
        path = self.base / "findings.md"
        path.write_text(
            "A dash \N{EM DASH} here\nWe delve here.\nWait\N{HORIZONTAL ELLIPSIS}\n| cell... |\n",
            encoding="utf-8",
        )
        result = self.run_checker(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("4 findings in 1 files", result.stdout)

    def test_code_fence_skips_two_rules(self):
        path = self.base / "fenced.md"
        path.write_text(
            "```text\nA dash \N{EM DASH} here\nWe delve here.\nWait\N{HORIZONTAL ELLIPSIS}\n"
            "| cell... |\n```\n",
            encoding="utf-8",
        )
        result = self.run_checker(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 findings in 1 files", result.stdout)

    def test_yaml_description_markdown(self):
        path = self.base / "value.yml"
        path.write_text(
            'description: "Uses **bold** and `code`"\n', encoding="utf-8"
        )
        result = self.run_checker(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("1 findings in 1 files", result.stdout)
        self.assertIn("markdown-in-descriptions", result.stdout)

    def test_clean_file(self):
        path = self.base / "clean.md"
        path.write_text("Plain, direct prose.\n", encoding="utf-8")
        result = self.run_checker(path)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("0 findings in 1 files", result.stdout)

    def test_command_options_are_not_dash_findings(self):
        path = self.base / "options.md"
        path.write_text("Use --flag and --dry-run in prose.\n", encoding="utf-8")
        result = self.run_checker(path)
        self.assertEqual(result.returncode, 0, result.stdout)


WRAPPED = (
    "This paragraph has been split across several lines\n"
    "and its next line continues the same thought\n"
    "with a final line ending the paragraph."
)


class HardWrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        checker = REPO / "checkers" / "check_writing.py"
        if not checker.is_file():
            raise unittest.SkipTest("writing checker is absent from the repository")
        spec = importlib.util.spec_from_file_location("check_writing", checker)
        cls.check_writing = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.check_writing)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()

    def test_wrapped_paragraph_is_reported_once(self):
        self.assertEqual(
            self.check_writing.hard_wrap_findings(WRAPPED),
            [(1, WRAPPED.splitlines()[0])],
        )

    def test_sentences_on_separate_lines_are_clean(self):
        text = "This sentence ends with a full stop.\nand this sentence does too."
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_list_items_are_clean(self):
        for marker in ("-", "*", "+", "1.", "  - [ ]", "  - [x]"):
            with self.subTest(marker=marker):
                text = marker + " " + WRAPPED.splitlines()[0] + "\nand a continuation."
                self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_fenced_blocks_are_clean(self):
        for marker in ("```", "~~~~"):
            with self.subTest(marker=marker):
                text = marker + "text\n" + WRAPPED + "\n" + marker
                self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_fence_closing_matches_marker_and_length(self):
        text = "````text\n```\n" + WRAPPED + "\n~~~~\n" + WRAPPED
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [])
        text += "\n````\n\n" + WRAPPED
        self.assertEqual(self.check_writing.hard_wrap_findings(text)[0][0], 12)

    def test_table_and_quote_are_clean(self):
        for marker in ("|", ">"):
            with self.subTest(marker=marker):
                text = "\n".join(marker + " " + line for line in WRAPPED.splitlines())
                self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_explicit_line_breaks_are_clean(self):
        for ending in ("  ", "\\"):
            with self.subTest(ending=ending):
                text = ending.join([WRAPPED.splitlines()[0], "\nand a continuation."])
                self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_front_matter_is_clean(self):
        text = "-" * 3 + "\ndescription: >\n" + "\n".join("  " + line for line in WRAPPED.splitlines()) + "\n" + "-" * 3
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [])
        self.assertEqual(self.check_writing.hard_wrap_findings(text + "\n\n" + WRAPPED)[0][0], 8)

    def test_short_line_is_clean(self):
        self.assertEqual(self.check_writing.hard_wrap_findings("A" * 25 + "\na continuation"), [])

    def test_length_boundaries(self):
        for length, expected in ((29, 0), (30, 1), (110, 1), (111, 0)):
            with self.subTest(length=length):
                findings = self.check_writing.hard_wrap_findings("A" * length + "\na continuation")
                self.assertEqual(len(findings), expected)
                if findings:
                    self.assertEqual(findings[0][1], "A" * length)

    def test_terminal_punctuation_is_clean(self):
        for punctuation in ".!?:;":
            for closing in ("", '"', "'", ")", "]", "*", "_"):
                with self.subTest(punctuation=punctuation, closing=closing):
                    text = "A" * 30 + punctuation + closing + "\na continuation"
                    self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_multiple_closers_and_curly_quotes_are_clean(self):
        for ending in ('."', '.")', '.**', '.[^1]', '.\u201d', '.\u2019', '.\u201d)', '.**[^note]'):
            with self.subTest(ending=ending):
                text = "A" * 30 + ending + "\na continuation"
                self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_setext_heading_is_clean(self):
        text = "Heading\n" + "=" * 40 + "\na paragraph below the heading."
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_list_continuations_are_clean(self):
        text = "- A bullet\n" + "\n".join("  " + line for line in WRAPPED.splitlines())
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_blank_line_ends_list_context(self):
        text = "- A bullet\n  A continuation.\n\n" + WRAPPED
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [(4, WRAPPED.splitlines()[0])])

    def test_heading_separates_findings_without_blank_lines(self):
        text = "\n".join("## Heading\n" + WRAPPED for _ in range(3))
        self.assertEqual([line for line, _ in self.check_writing.hard_wrap_findings(text)], [2, 6, 10])

    def test_thematic_break_does_not_hide_prose(self):
        text = "-" * 3 + "\n" + WRAPPED + "\n\n" + "-" * 3 + "\n" + WRAPPED
        self.assertEqual([line for line, _ in self.check_writing.hard_wrap_findings(text)], [2, 7])

    def test_crlf_matches_lf(self):
        self.assertEqual(
            self.check_writing.hard_wrap_findings(WRAPPED.replace("\n", "\r\n")),
            self.check_writing.hard_wrap_findings(WRAPPED),
        )

    def test_trailing_newline_is_optional(self):
        self.assertEqual(self.check_writing.hard_wrap_findings(WRAPPED), [(1, WRAPPED.splitlines()[0])])
        self.assertEqual(
            self.check_writing.hard_wrap_findings(WRAPPED + "\n"),
            self.check_writing.hard_wrap_findings(WRAPPED),
        )

    def test_findings_are_sorted_by_line(self):
        path = self.base / "ordered.md"
        path.write_text(WRAPPED + "\n\nWe delve here.\n", encoding="utf-8")
        self.assertEqual(self.check_writing.inspect_file(path, self.check_writing.RULES), [
            (1, "hard-wrap", WRAPPED.splitlines()[0]),
            (5, "ai-phrasing (delve)", "We delve here."),
        ])

    def test_continuation_starts(self):
        for start, expected in (("lowercase", 1), ("The next", 0), ("AND next", 0), ("Other next", 0), ("Theatre next", 0)):
            with self.subTest(start=start):
                self.assertEqual(len(self.check_writing.hard_wrap_findings("A" * 30 + "\n" + start)), expected)

    def test_other_markdown_lines_are_clean(self):
        for line in (
            "# " + "A" * 30, "    " + "A" * 30, "\t" + "A" * 30,
            "<div>" + "A" * 30, "-" * 30, "*" * 30, "_" * 30,
            "[label]: " + "A" * 30,
        ):
            with self.subTest(line=line):
                self.assertEqual(self.check_writing.hard_wrap_findings(line + "\na continuation"), [])
                self.assertEqual(self.check_writing.hard_wrap_findings("A" * 30 + "\n" + line), [])

    def test_skipped_line_does_not_bridge_pair(self):
        text = "A" * 30 + "\n# Heading\na continuation"
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_each_paragraph_is_reported(self):
        self.assertEqual(
            [line for line, excerpt in self.check_writing.hard_wrap_findings(WRAPPED + "\n\n" + WRAPPED)],
            [1, 5],
        )

    def test_yaml_is_clean(self):
        for suffix in (".yaml", ".yml"):
            with self.subTest(suffix=suffix):
                path = self.base / ("draft" + suffix)
                path.write_text(WRAPPED, encoding="utf-8")
                self.assertEqual(self.check_writing.inspect_file(path, {"hard-wrap"}), [])

    def test_hard_wrap_is_opt_in(self):
        path = self.base / "draft.md"
        path.write_text(WRAPPED, encoding="utf-8")
        self.assertEqual(self.check_writing.parse_args([str(path)]).rules, self.check_writing.DEFAULT_RULES)
        self.assertNotIn("hard-wrap", self.check_writing.DEFAULT_RULES)
        self.assertEqual(self.check_writing.inspect_file(path, self.check_writing.DEFAULT_RULES), [])
        self.assertEqual(self.check_writing.inspect_file(path, self.check_writing.RULES), [(1, "hard-wrap", WRAPPED.splitlines()[0])])

    def test_unavailable_hard_wrap_reports_configuration_error(self):
        output = io.StringIO()
        with mock.patch.object(self.check_writing, "hard_wrap_findings", None):
            with contextlib.redirect_stderr(output):
                result = self.check_writing.main(["--rules", "hard-wrap", str(self.base)])
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "check-writing: hard-wrap rule needs claude/hooks/_common.py next to this checker\n")

    def test_inspect_file_rejects_unavailable_helper(self):
        with mock.patch.object(self.check_writing, "hard_wrap_findings", None):
            with self.assertRaisesRegex(RuntimeError, "hard-wrap rule needs claude/hooks/_common.py"):
                self.check_writing.inspect_file(self.base / "draft.md", {"hard-wrap"})

    def test_main_converts_runtime_error_to_exit_two(self):
        path = self.base / "draft.md"
        path.write_text("Clean.", encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(self.check_writing, "inspect_file", side_effect=RuntimeError(self.check_writing.HARD_WRAP_UNAVAILABLE)):
            with contextlib.redirect_stderr(output):
                result = self.check_writing.main(["--rules", "hard-wrap", str(path)])
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), self.check_writing.HARD_WRAP_UNAVAILABLE + "\n")

    def test_broken_helper_import_is_caught(self):
        for source in (b"invalid syntax here", b"\xff"):
            with self.subTest(source=source):
                checker = self.base / "checkers" / "check_writing.py"
                checker.parent.mkdir(exist_ok=True)
                shutil.copy2(self.check_writing.__file__, checker)
                hooks = self.base / "claude" / "hooks"
                hooks.mkdir(parents=True, exist_ok=True)
                (hooks / "_common.py").write_bytes(source)
                path = self.base / "draft.md"
                path.write_text("Clean.", encoding="utf-8")
                for arguments, expected in (([], 0), (["--rules", "hard-wrap"], 2)):
                    result = subprocess.run(
                        [sys.executable, "-I", str(checker)] + arguments + [str(path)],
                        cwd=self.base, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_list_context_ends_at_heading_fence_or_prose(self):
        for separator in ("## Heading", "```\n```", "A separate paragraph."):
            with self.subTest(separator=separator):
                text = "- Bullet\n" + separator + "\n"
                text += "\n".join("  " + line for line in WRAPPED.splitlines())
                expected_line = 3 + separator.count("\n")
                self.assertEqual(self.check_writing.hard_wrap_findings(text), [(expected_line, WRAPPED.splitlines()[0])])

    def test_defaults_work_without_hard_wrap_helper(self):
        path = self.base / "draft.md"
        path.write_text(WRAPPED, encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(self.check_writing, "hard_wrap_findings", None):
            with contextlib.redirect_stdout(output):
                result = self.check_writing.main([str(path)])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "0 findings in 1 files\n")

    def test_standalone_checker(self):
        checker = self.base / "check_writing.py"
        shutil.copy2(self.check_writing.__file__, checker)
        path = self.base / "draft.md"
        path.write_text(WRAPPED, encoding="utf-8")
        for arguments, expected in (([], 0), (["--rules", "hard-wrap"], 2)):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-I", str(checker)] + arguments + [str(path)],
                    cwd=self.base, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if expected == 2:
                    self.assertIn("hard-wrap rule needs claude/hooks/_common.py", result.stderr)

    def test_front_matter_with_blank_line_and_folded_description(self):
        text = "-" * 3 + "\ntitle: Draft\n\ndescription: >\n"
        text += "\n".join("  " + line for line in WRAPPED.splitlines())
        text += "\n# Comment\ntags:\n- draft\n" + "-" * 3
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [])

    def test_prose_between_thematic_breaks_is_reported(self):
        text = "-" * 3 + "\n" + WRAPPED + "\n" + "-" * 3
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [(2, WRAPPED.splitlines()[0])])

    def test_front_matter_closing_marker_has_line_limit(self):
        for closing_line, expected in ((60, []), (61, [(2, "title: " + "A" * 30)])):
            with self.subTest(closing_line=closing_line):
                text = "-" * 3 + "\ntitle: " + "A" * 30 + "\nother: value\n"
                text += "\n" * (closing_line - 4) + "-" * 3
                self.assertEqual(self.check_writing.hard_wrap_findings(text), expected)

    def test_two_line_paragraph_is_reported(self):
        text = "intro\n\n" + "\n".join(WRAPPED.splitlines()[:2])
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [(3, WRAPPED.splitlines()[0])])

    def test_first_wrap_pair_is_reported(self):
        text = "intro\n\n" + WRAPPED.splitlines()[0] + "\nand a short line\n"
        text += WRAPPED.splitlines()[1] + "\nand a final short line"
        self.assertEqual(self.check_writing.hard_wrap_findings(text), [(3, WRAPPED.splitlines()[0])])

    def run_checker(self, text):
        path = self.base / "draft.txt"
        path.write_text(text, encoding="utf-8")
        return path, subprocess.run(
            [sys.executable, str(Path(self.check_writing.__file__)), "--rules", "hard-wrap", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )

    def test_cli_reports_one_finding(self):
        path, result = self.run_checker(WRAPPED)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"{path}:1: hard-wrap: {WRAPPED.splitlines()[0]}",
            "1 findings in 1 files",
        ])

    def test_cli_clean_file(self):
        path, result = self.run_checker("A single line paragraph.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "0 findings in 1 files\n")


def read_json_test(path):
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
