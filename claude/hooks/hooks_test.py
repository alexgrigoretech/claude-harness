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
        self.base = Path(self.temporary.name)

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
        self.path = Path(temporary.name) / "nested" / "shared.json"

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
                    self.assertNotEqual(acquisitions[0], acquisitions[1])
                    self.assertEqual(acquisitions[-1], (current.st_ino, current.st_dev))


class HookCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.home = Path(cls.temporary.name)

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
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)

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
        self.assertEqual(settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "opus")
        self.assertEqual(settings["cleanupPeriodDays"], 90)
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
        local = {"gh_account": "someone", "identities": {"default": "someone@example.invalid"}}
        local_path.write_text(json.dumps(local), encoding="utf-8")
        original = local_path.read_bytes()
        tracked = read_json_test(copied / "machines" / f"{TEST_MACHINE}.json")
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.run_install(home, repo=copied)
                self.assertEqual(result.returncode, 0, result.stderr)
                machine = read_json_test(home / ".claude" / "local" / "machine.json")
                self.assertEqual(machine["gh_account"], "someone")
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
        self.assertTrue(all("(unchanged)" in line or "(skipped)" in line for line in statuses))
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
                         if value else f"1. Unpack this folder as the harness install folder named in machines/{TEST_MACHINE}.md. Replace the previous folder entirely; keep nothing from it.")
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
                            f"1. Unpack this folder as the harness install folder named in machines/{TEST_MACHINE}.md. Replace the previous folder entirely; keep nothing from it.\n"
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


class CommitMsgGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

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


class GitHooksInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
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
        self.base = Path(self.temporary.name)

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
        self.base = Path(self.temporary.name)

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
