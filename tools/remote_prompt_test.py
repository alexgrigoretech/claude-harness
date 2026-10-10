import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import remote_prompt as remote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


RESULT = {
    "result": "The answer",
    "session_id": "test-session",
    "num_turns": 2,
    "duration_ms": 1234,
    "total_cost_usd": 0.01234,
    "terminal_reason": "completed",
}
RUN = "20261004-120000-abcd"
SYSTEM_FLAGS = ["--append-system-prompt", remote.HEADLESS_PREAMBLE]
SYSTEM_SUFFIX = " " + shlex.join(SYSTEM_FLAGS)


class RemotePromptTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.machine = self.home / "machine.json"
        self.sessions = self.home / ".claude/local/remote-sessions.json"
        self.configure({"ssh": "test-alias", "cwd": "~/work/repo"})
        self.process = mock.patch.object(remote.subprocess, "run").start()
        self.addCleanup(mock.patch.stopall)
        self.process.return_value = subprocess.CompletedProcess([], 0, json.dumps(RESULT).encode(), b"")
        self.which = mock.patch.object(remote.shutil, "which", return_value="/resolved/claude").start()

    def configure(self, entry):
        self.machine.write_text(json.dumps({"remote": {"test": entry}}), encoding="utf-8")

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = remote.main(["test", "--machine-json", str(self.machine), "--home", str(self.home), *arguments])
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def seed_sessions(self, machines=None):
        self.sessions.parent.mkdir(parents=True, exist_ok=True)
        self.sessions.write_text(json.dumps({"schema": 1, "machines": machines or {
            "test": {"session_id": "previous-session", "run": RUN, "cwd": "~/previous", "at": 123, "collected": True},
        }}), encoding="utf-8")

    def test_ssh_with_shell(self):
        self.configure({"ssh": "test-alias", "shell": "zsh -ic", "cwd": "~/work/repo"})
        self.assertEqual(self.invoke("hello")[0], 0)
        self.assertEqual(self.process.call_args.args[0], [
            "ssh", "-o", "BatchMode=yes", "test-alias",
            "zsh -ic " + shlex.quote("cd ~/work/repo && claude -p --output-format json --model claude-opus-5-5" + SYSTEM_SUFFIX),
        ])
        self.assertEqual(self.process.call_args.kwargs, {"input": b"hello", "capture_output": True, "timeout": 600})

    def test_ssh_without_shell(self):
        self.assertEqual(self.invoke("hello", "world")[0], 0)
        self.assertEqual(self.process.call_args.args[0][-1], "cd ~/work/repo && claude -p --output-format json --model claude-opus-5-5" + SYSTEM_SUFFIX)
        self.assertEqual(self.process.call_args.kwargs["input"], b"hello world")

    def test_remote_paths_quote_spaces_and_keep_tilde(self):
        self.configure({"ssh": "test-alias", "cwd": "~/a b's", "claude": "~/bin/claude-proxy"})
        self.invoke("hello")
        self.assertEqual(self.process.call_args.args[0][-1],
                         "cd ~/" + shlex.quote("a b's") + " && ~/bin/claude-proxy -p --output-format json --model claude-opus-5-5" + SYSTEM_SUFFIX)

    def test_ssh_rejects_windows_cwd_override(self):
        for cwd in ("C:/Program Files/Git/srv/app", "f:folder", "C:\\Users\\test", "folder\\child"):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.invoke("hello", "--cwd", cwd), (
                    2, "", f"remote test: --cwd {cwd} looks like a local Windows path; from Git Bash prefix the command with MSYS_NO_PATHCONV=1 and quote the value\n",
                ))
        self.process.assert_not_called()
        self.assertFalse(self.sessions.exists())

    def test_ssh_preserves_posix_cwd_override(self):
        for cwd in ("/srv/app", "~/x"):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.invoke("hello", "--cwd", cwd)[0], 0)
                self.assertTrue(self.process.call_args.args[0][-1].startswith(f"cd {cwd} && "))

    def test_local_accepts_windows_cwd_override(self):
        self.configure({})
        self.assertEqual(self.invoke("hello", "--cwd", "F:/harness")[0], 0)
        self.assertEqual(self.process.call_args.kwargs["cwd"], str(Path("F:/harness")))

    def test_local_resolution_and_environment(self):
        self.configure({"cwd": "~", "claude": "~/bin/claude"})
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "REMOTE_TEST": "kept"}):
            self.assertEqual(self.invoke("hello")[0], 0)
            self.assertEqual(os.environ["CLAUDECODE"], "1")
        self.which.assert_called_once_with(str(Path("~/bin/claude").expanduser()))
        self.assertEqual(self.process.call_args.args[0], ["/resolved/claude", "-p", "--output-format", "json", "--model", "claude-opus-5-5", *SYSTEM_FLAGS])
        kwargs = self.process.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(Path.home()))
        self.assertNotIn("CLAUDECODE", kwargs["env"])
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", kwargs["env"])
        self.assertEqual(kwargs["env"]["REMOTE_TEST"], "kept")

    def test_model_defaults_in_generated_argv(self):
        self.configure({})
        self.assertEqual(self.invoke("hello")[0], 0)
        argv = self.process.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv.index("--model"), argv.index("--output-format") + 2)

    def test_model_override_in_generated_argv(self):
        self.configure({"model": "claude-sonnet-5-5 "})
        self.assertEqual(self.invoke("hello", "--resume", "chosen-session")[0], 0)
        argv = self.process.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertLess(argv.index("--model"), argv.index("--resume"))

    def test_model_invalid_values_are_refused(self):
        for value in (123, None, False, [], {}, "", " "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    remote.validate_remote_block({"test": {"model": value}})
                self.assertEqual(str(raised.exception), "remote.test.model must be a non empty string")

    def test_local_default_cwd(self):
        self.configure({})
        self.invoke("hello")
        self.assertEqual(self.process.call_args.kwargs["cwd"], str(Path.cwd()))

    def test_local_missing_claude(self):
        self.configure({})
        self.which.return_value = None
        code, _, stderr = self.invoke("hello")
        self.assertEqual(code, 2)
        self.assertIn("remote test: claude not found on PATH", stderr)
        self.process.assert_not_called()

    def test_configured_flags_and_overrides(self):
        self.configure({"ssh": "test-alias", "permission_mode": "plan", "max_turns": 3, "timeout": 12})
        self.invoke("hello")
        self.assertIn("--permission-mode plan --max-turns 3", self.process.call_args.args[0][-1])
        self.assertEqual(self.process.call_args.kwargs["timeout"], 12)
        self.invoke("hello", "--permission-mode", "auto", "--max-turns", "5", "--max-budget-usd", "1.25", "--timeout", "20", "--cwd", "/tmp")
        self.assertEqual(self.process.call_args.args[0][-1],
                         "cd /tmp && claude -p --output-format json --model claude-opus-5-5 --permission-mode auto --max-turns 5 --max-budget-usd 1.25" + SYSTEM_SUFFIX)
        self.assertEqual(self.process.call_args.kwargs["timeout"], 20)

    def test_continue(self):
        self.seed_sessions()
        self.assertEqual(self.invoke("follow up", "--continue")[0], 0)
        self.assertIn("--resume previous-session", self.process.call_args.args[0][-1])
        recorded = json.loads(self.sessions.read_text())["machines"]["test"]
        self.assertEqual(recorded["session_id"], "test-session")
        self.assertEqual(recorded["run"], RUN)

    def test_continue_without_record(self):
        code, _, stderr = self.invoke("hello", "--continue")
        self.assertEqual(code, 2)
        self.assertIn("test: no session recorded", stderr)
        self.process.assert_not_called()

    def test_continue_refuses_uncollected_detach_but_resume_is_allowed(self):
        self.seed_sessions()
        code, stdout, _ = self.invoke("hello", "--detach", "--continue")
        self.assertEqual(code, 0)
        run = stdout.strip()
        record = json.loads(self.sessions.read_text())["machines"]["test"]
        self.assertFalse(record["collected"])
        self.assertEqual(record["session_id"], "previous-session")
        self.process.reset_mock()
        self.assertEqual(self.invoke("follow up", "--continue"), (
            2, "", f"remote test: run {run} not collected yet; run --collect first or pass --resume <id>\n",
        ))
        self.process.assert_not_called()
        self.assertEqual(self.invoke("follow up", "--resume", "explicit-session")[0], 0)
        self.assertIn("--resume explicit-session", self.process.call_args.args[0][-1])
        self.assertFalse(json.loads(self.sessions.read_text())["machines"]["test"]["collected"])

    def test_collect_result_allows_continue_with_new_session(self):
        remote.record_session(self.home, "test", "~", "old-session", RUN, collected=False)
        self.assertEqual(self.invoke("--collect")[0], 0)
        self.assertTrue(json.loads(self.sessions.read_text())["machines"]["test"]["collected"])
        self.assertEqual(self.invoke("follow up", "--continue")[0], 0)
        self.assertIn("--resume test-session", self.process.call_args.args[0][-1])

    def test_legacy_run_without_collection_state_requires_collect(self):
        self.seed_sessions({"test": {"session_id": "old-session", "run": RUN}})
        self.assertEqual(self.invoke("follow up", "--continue")[0], 2)
        self.process.assert_not_called()

    def test_collect_running_keeps_continue_blocked(self):
        remote.record_session(self.home, "test", "~", "old-session", RUN, collected=False)
        self.process.return_value.stdout = b"\n\x1e\n\n\x1e\n"
        self.assertEqual(self.invoke("--collect")[0], 4)
        self.assertFalse(json.loads(self.sessions.read_text())["machines"]["test"]["collected"])
        self.assertEqual(self.invoke("follow up", "--continue")[0], 2)

    def test_collect_failed_run_marks_collected(self):
        remote.record_session(self.home, "test", "~", "old-session", RUN, collected=False)
        self.process.return_value.stdout = b"\n\x1e\nfailed\n\x1e\n2\n"
        self.assertEqual(self.invoke("--collect")[0], 3)
        self.assertTrue(json.loads(self.sessions.read_text())["machines"]["test"]["collected"])
        self.process.return_value.stdout = json.dumps(RESULT).encode()
        self.assertEqual(self.invoke("follow up", "--continue")[0], 0)
        self.assertIn("--resume old-session", self.process.call_args.args[0][-1])

    def test_explicit_resume_and_mutual_exclusion(self):
        self.assertEqual(self.invoke("hello", "--resume", "explicit-id")[0], 0)
        self.assertIn("--resume explicit-id", self.process.call_args.args[0][-1])
        self.assertEqual(self.invoke("hello", "--resume", "explicit-id", "--continue")[0], 2)

    def test_result_footer_and_session_merge(self):
        self.seed_sessions({"other": {"session_id": "untouched", "run": None}})
        code, stdout, stderr = self.invoke("hello")
        self.assertEqual((code, stdout), (0, "The answer\n"))
        self.assertEqual(stderr, "remote test: session test-session, 2 turns, 1.2 s, $0.0123, completed\n")
        records = json.loads(self.sessions.read_text())["machines"]
        self.assertEqual(records["other"]["session_id"], "untouched")
        self.assertEqual(records["test"]["cwd"], "~/work/repo")
        self.assertIsNone(records["test"]["run"])
        self.assertIsInstance(records["test"]["at"], int)
        self.assertFalse(list(self.sessions.parent.glob("*.tmp")))

    def test_record_session_merges_despite_stale_caller_state(self):
        stale = remote.load_sessions(self.home)
        remote.record_session(self.home, "first", "~/first", "first-session")
        self.assertEqual(stale["machines"], {})
        remote.record_session(self.home, "second", "~/second", "second-session", RUN)
        records = json.loads(self.sessions.read_text())["machines"]
        self.assertEqual(records["first"]["session_id"], "first-session")
        self.assertEqual(records["second"]["session_id"], "second-session")
        self.assertEqual(records["second"]["run"], RUN)

    def test_record_session_reloads_and_writes_under_lock(self):
        locked = False
        load = remote.load_sessions
        write = remote.atomic_write_text

        @contextlib.contextmanager
        def lock(path):
            nonlocal locked
            self.assertEqual(path, self.sessions)
            locked = True
            yield
            locked = False

        def checked_load(home):
            self.assertTrue(locked)
            return load(home)

        def checked_write(path, value):
            self.assertTrue(locked)
            return write(path, value)

        with mock.patch.object(remote, "file_lock", lock), mock.patch.object(remote, "load_sessions", checked_load), mock.patch.object(remote, "atomic_write_text", checked_write):
            remote.record_session(self.home, "test", "~", "session")
        self.assertFalse(locked)

    def test_concurrent_machine_update_survives_running_prompt(self):
        self.seed_sessions()

        def complete(*args, **kwargs):
            remote.record_session(self.home, "other", "~/other", "other-session")
            return subprocess.CompletedProcess([], 0, json.dumps(RESULT).encode(), b"")

        self.process.side_effect = complete
        self.assertEqual(self.invoke("hello", "--continue")[0], 0)
        records = json.loads(self.sessions.read_text())["machines"]
        self.assertEqual(records["other"]["session_id"], "other-session")
        self.assertEqual(records["test"]["session_id"], "test-session")

    def test_last_json_object_and_existing_newline(self):
        result = dict(RESULT, result="answer\n")
        self.process.return_value.stdout = b'noise\n{"result": "old"}\n' + json.dumps(result).encode() + b"\n[]\ntrailer"
        self.assertEqual(self.invoke("hello")[:2], (0, "answer\n"))

    def test_missing_result(self):
        result = dict(RESULT)
        del result["result"]
        self.process.return_value.stdout = json.dumps(result).encode()
        code, stdout, stderr = self.invoke("hello")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(stdout), result)
        self.assertIn("session test-session", stderr)

    def test_null_result_and_numbers_record_session(self):
        result = dict(RESULT, result=None, duration_ms=None, total_cost_usd=None)
        self.process.return_value.stdout = json.dumps(result).encode()
        code, stdout, stderr = self.invoke("hello")
        self.assertEqual(code, 3)
        self.assertEqual(stdout, json.dumps(result, indent=2) + "\n")
        self.assertIn("0.0 s, $0.0000", stderr)
        self.assertEqual(json.loads(self.sessions.read_text())["machines"]["test"]["session_id"], "test-session")

    def test_non_string_result_is_printed_as_string(self):
        for answer in (42, False, ["answer"], {"answer": "value"}):
            with self.subTest(answer=answer):
                self.process.return_value.stdout = json.dumps(dict(RESULT, result=answer)).encode()
                self.assertEqual(self.invoke("hello")[:2], (0, str(answer) + "\n"))
                self.assertEqual(json.loads(self.sessions.read_text())["machines"]["test"]["session_id"], "test-session")

    def test_malformed_metadata_does_not_lose_session(self):
        for denials in (True, [None, 7], {"tool_name": "Bash"}):
            with self.subTest(denials=denials):
                result = dict(RESULT, duration_ms=[], total_cost_usd="bad", permission_denials=denials)
                self.process.return_value.stdout = json.dumps(result).encode()
                code, _, stderr = self.invoke("hello")
                self.assertEqual(code, 0)
                self.assertIn("0.0 s, $0.0000", stderr)
                self.assertEqual(json.loads(self.sessions.read_text())["machines"]["test"]["session_id"], "test-session")

    def test_is_error(self):
        self.process.return_value.stdout = json.dumps(dict(RESULT, is_error=True)).encode()
        self.assertEqual(self.invoke("hello")[0], 1)

    def test_denials(self):
        tool_input = {"command": "x" * 160}
        result = dict(RESULT, permission_denials=[{"tool_name": "Bash", "tool_input": tool_input, "tool_use_id": "use-id"}, {}])
        self.process.return_value.stdout = json.dumps(result).encode()
        code, _, stderr = self.invoke("hello")
        self.assertEqual(code, 0)
        self.assertIn("permission denied 2 times:\nBash " + json.dumps(tool_input)[:120] + " use-id\n", stderr)
        self.assertNotIn("x" * 121, stderr)

    def test_optional_result_fields(self):
        self.process.return_value.stdout = b'{"result":"ok", "session_id":"s", "duration_ms":0, "total_cost_usd":0}'
        code, _, stderr = self.invoke("hello")
        self.assertEqual(code, 0)
        self.assertIn("? turns", stderr)

    def test_invalid_sessions_replaced_with_warning(self):
        self.sessions.parent.mkdir(parents=True)
        for content in ("bad json", "[]", '{"schema":1,"machines":{"test":7}}'):
            with self.subTest(content=content):
                self.sessions.write_text(content)
                code, _, stderr = self.invoke("hello")
                self.assertEqual(code, 0)
                self.assertIn("warning: unreadable or invalid", stderr)
                self.assertEqual(json.loads(self.sessions.read_text())["schema"], 1)

    def test_unreadable_sessions_replaced(self):
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.sessions:
                raise PermissionError("test unreadable")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read):
            code, _, stderr = self.invoke("hello")
        self.assertEqual(code, 0)
        self.assertIn("warning: unreadable or invalid", stderr)
        self.assertTrue(self.sessions.is_file())

    def test_prompt_file_verbatim(self):
        prompt = self.home / "prompt.md"
        content = "/auto a 'quoted' goal\r\nwith Unicode \u00e9\n"
        prompt.write_bytes(content.encode("utf-8"))
        self.assertEqual(self.invoke("--prompt-file", str(prompt))[0], 0)
        self.assertEqual(self.process.call_args.kwargs["input"], content.encode("utf-8"))
        self.assertNotIn(content, str(self.process.call_args.args))
        self.assertEqual(self.invoke("hello", "--prompt-file", str(prompt))[0], 2)

    def test_no_preamble_foreground(self):
        self.assertEqual(self.invoke("hello", "--no-preamble")[0], 0)
        self.assertNotIn("--append-system-prompt", self.process.call_args.args[0][-1])
        self.assertEqual(self.process.call_args.kwargs["input"], b"hello")

    def test_no_preamble_detached(self):
        for entry in ({"ssh": "test-alias"}, {}):
            with self.subTest(entry=entry), mock.patch.object(remote.subprocess, "Popen") as popen:
                self.configure(entry)
                code, stdout, _ = self.invoke("hello", "--detach", "--no-preamble")
                self.assertEqual(code, 0)
                if "ssh" in entry:
                    self.assertEqual(self.process.call_args.kwargs["input"], b"hello")
                    self.assertNotIn("--append-system-prompt", self.process.call_args.args[0][-1])
                else:
                    self.assertTrue(remote.parse_args(popen.call_args.args[0][2:]).no_preamble)
                    path = self.home / ".claude/local/remote-runs" / (stdout.strip() + ".prompt")
                    self.assertEqual(path.read_bytes(), b"hello")

    def test_detach_system_prompt_round_trips_shell_quoting(self):
        self.assertNotIn("'", remote.HEADLESS_PREAMBLE)
        self.assertNotIn("$", remote.HEADLESS_PREAMBLE)
        for entry in ({"ssh": "test-alias"}, {"ssh": "test-alias", "shell": "zsh -ic"}):
            with self.subTest(entry=entry):
                self.configure(entry)
                self.assertEqual(self.invoke("/auto goal", "--detach")[0], 0)
                command = shlex.split(self.process.call_args.args[0][-1])
                worker = shlex.split(command[command.index("(nohup") + 3])
                claude = shlex.split(worker[2])
                self.assertEqual(claude[claude.index("--append-system-prompt") + 1], remote.HEADLESS_PREAMBLE)
                self.assertEqual(claude.count("--append-system-prompt"), 1)
                self.assertEqual(self.process.call_args.kwargs["input"], b"/auto goal")

    def test_system_prompt_on_continue_and_resume(self):
        self.seed_sessions()
        for flags in (("--continue",), ("--resume", "chosen-session")):
            with self.subTest(flags=flags):
                self.assertEqual(self.invoke("/ship", *flags)[0], 0)
                argv = shlex.split(self.process.call_args.args[0][-1])
                self.assertEqual(argv[-2:], SYSTEM_FLAGS)
                self.assertEqual(self.process.call_args.kwargs["input"], b"/ship")

    def test_worker_no_preamble(self):
        self.configure({})
        self.worker_files()
        self.assertEqual(self.invoke("--worker", RUN, "--no-preamble")[0], 0)
        self.assertNotIn("--append-system-prompt", self.process.call_args.args[0])

    def test_empty_prompt_and_invalid_cli_values(self):
        self.assertEqual(self.invoke()[0], 2)
        for flag, value in (("--max-turns", "0"), ("--timeout", "-1"), ("--max-budget-usd", "nan"), ("--permission-mode", "bad")):
            with self.subTest(flag=flag):
                self.assertEqual(self.invoke("hello", flag, value)[0], 2)

    def test_timeout(self):
        self.process.side_effect = subprocess.TimeoutExpired("ssh", 9)
        code, _, stderr = self.invoke("hello", "--timeout", "9")
        self.assertEqual(code, 124)
        self.assertEqual(stderr, "remote test: timed out after 9 s (the run may still be going; use --detach for long prompts)\n")

    def test_ssh_failure(self):
        self.process.return_value = subprocess.CompletedProcess([], 255, b"", b"  connection failed\n")
        self.assertEqual(self.invoke("hello"), (255, "", "connection failed\n"))

    def test_missing_json(self):
        self.process.return_value.stdout = b"not json"
        self.assertEqual(self.invoke("hello")[0], 3)

    def test_detach_with_shell(self):
        self.configure({"ssh": "test-alias", "shell": "zsh -ic"})
        self.process.return_value.stdout = (RUN + "\n").encode()
        code, stdout, stderr = self.invoke("/auto goal", "--detach")
        run = stdout.strip()
        self.assertEqual(code, 0)
        self.assertRegex(run, r"^\d{8}-\d{6}-[0-9a-f]{4}$")
        root = "~/.claude/local/remote-runs/" + run
        worker = f"zsh -ic {shlex.quote('cd ~ && claude -p --output-format json --model claude-opus-5-5' + SYSTEM_SUFFIX)} < {root}.prompt > {root}.json 2> {root}.err; echo $? > {root}.done"
        self.assertEqual(self.process.call_args.args[0][-1],
                         f"mkdir -p ~/.claude/local/remote-runs && cat > {root}.prompt && "
                         f"(nohup sh -c {shlex.quote(worker)} > /dev/null 2>&1 &) && echo {run}")
        self.assertIn(f"remote test: detached run {run}", stderr)
        self.assertEqual(self.process.call_args.kwargs["input"], b"/auto goal")
        record = json.loads(self.sessions.read_text())["machines"]["test"]
        self.assertEqual(record["run"], run)
        self.assertIsNone(record["session_id"])

    def test_detach_without_shell(self):
        _, stdout, _ = self.invoke("hello", "--detach")
        self.assertEqual(self.process.call_args.kwargs["input"], b"hello")
        root = "~/.claude/local/remote-runs/" + stdout.strip()
        worker = f"sh -c {shlex.quote('cd ~/work/repo && claude -p --output-format json --model claude-opus-5-5' + SYSTEM_SUFFIX)} < {root}.prompt > {root}.json 2> {root}.err; echo $? > {root}.done"
        self.assertIn("nohup sh -c " + shlex.quote(worker), self.process.call_args.args[0][-1])

    def test_local_detach(self):
        self.configure({})
        for platform in ("win32", "linux", "darwin"):
            with self.subTest(platform=platform), mock.patch.object(remote.sys, "platform", platform), mock.patch.object(remote.subprocess, "Popen") as popen:
                code, stdout, _ = self.invoke("/auto goal", "--detach")
                self.assertEqual(code, 0)
                root = self.home / ".claude/local/remote-runs"
                self.assertEqual((root / (stdout.strip() + ".prompt")).read_bytes(), b"/auto goal")
                worker_args = popen.call_args.args[0]
                self.assertEqual(worker_args[:3], [sys.executable, str(Path(remote.__file__).resolve()), "test"])
                parsed = remote.parse_args(worker_args[2:])
                self.assertEqual(parsed.worker, stdout.strip())
                self.assertEqual(parsed.home, self.home.resolve())
                self.assertEqual(parsed.machine_json, self.machine.resolve())
                self.assertEqual(parsed.cwd, str(Path.cwd()))
                kwargs = popen.call_args.kwargs
                if platform == "win32" and os.name == "nt":
                    self.assertEqual(kwargs["creationflags"], subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
                else:
                    self.assertTrue(kwargs["start_new_session"])
                self.assertNotIn("CLAUDECODE", kwargs["env"])
                self.assertNotIn("cwd", kwargs)
                popen.return_value.wait.assert_not_called()
                self.assertFalse(json.loads(self.sessions.read_text())["machines"]["test"]["collected"])
        self.process.assert_not_called()

    def test_detach_without_windows_constants_uses_new_session(self):
        self.configure({})
        with mock.patch.object(remote.sys, "platform", "win32"), mock.patch.dict(remote.subprocess.__dict__), mock.patch.object(remote.subprocess, "Popen") as popen:
            remote.subprocess.__dict__.pop("DETACHED_PROCESS", None)
            remote.subprocess.__dict__.pop("CREATE_NEW_PROCESS_GROUP", None)
            self.assertEqual(self.invoke("hello", "--detach")[0], 0)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertNotIn("creationflags", popen.call_args.kwargs)

    def test_local_detach_preserves_overrides(self):
        self.configure({"permission_mode": "plan", "max_turns": 3})
        self.seed_sessions()
        folder = self.home / "folder with spaces"
        with mock.patch.object(remote.subprocess, "Popen") as popen:
            self.assertEqual(self.invoke("hello", "--detach", "--continue", "--cwd", str(folder), "--permission-mode", "auto", "--max-turns", "8", "--max-budget-usd", "2.5")[0], 0)
        parsed = remote.parse_args(popen.call_args.args[0][2:])
        self.assertEqual(parsed.cwd, str(folder.resolve()))
        self.assertEqual(parsed.resume, "previous-session")
        self.assertEqual(parsed.permission_mode, "auto")
        self.assertEqual(parsed.max_turns, 8)
        self.assertEqual(parsed.max_budget_usd, "2.5")

    def test_local_detach_worker_rebuilds_claude_flags(self):
        self.configure({"permission_mode": "plan", "max_turns": 3})
        original_flags = remote.claude_flags
        for no_preamble in (False, True):
            with self.subTest(no_preamble=no_preamble), mock.patch.object(remote.subprocess, "Popen") as popen, mock.patch.object(remote, "claude_flags", side_effect=lambda *args: original_flags(*args) + ["--future-flag", "future"]):
                options = ["--no-preamble"] if no_preamble else []
                self.assertEqual(self.invoke("/auto goal", "--detach", *options)[0], 0)
                worker_args = popen.call_args.args[0][2:]
                self.assertEqual("--no-preamble" in worker_args, no_preamble)
                self.assertNotIn(remote.HEADLESS_PREAMBLE, worker_args)
                self.assertNotIn("--future-flag", worker_args)
                parsed = remote.parse_args(worker_args)
                self.assertEqual(parsed.permission_mode, "plan")
                self.assertEqual(parsed.max_turns, 3)
                self.assertEqual(remote.main(worker_args), 0)
                argv = self.process.call_args.args[0]
                self.assertEqual(argv[-2:], ["--future-flag", "future"])
                self.assertEqual("--append-system-prompt" in argv, not no_preamble)

    def worker_files(self):
        root = self.home / ".claude/local/remote-runs" / RUN
        root.parent.mkdir(parents=True, exist_ok=True)
        root.with_suffix(".prompt").write_bytes(b"/auto goal\r\n")
        return root

    def test_worker_success_writes_result_and_completion(self):
        self.configure({})
        root = self.worker_files()

        def complete(argv, **kwargs):
            self.assertEqual(kwargs["stdin"].read(), b"/auto goal\r\n")
            self.assertEqual(argv[-2:], SYSTEM_FLAGS)
            self.assertNotIn("CLAUDECODE", kwargs["env"])
            self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", kwargs["env"])
            self.assertNotIn("timeout", kwargs)
            self.assertFalse(root.with_suffix(".done").exists())
            kwargs["stdout"].write(json.dumps(RESULT).encode())
            return subprocess.CompletedProcess(argv, 0)

        self.process.side_effect = complete
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}):
            self.assertEqual(self.invoke("--worker", RUN)[0], 0)
        self.assertEqual(root.with_suffix(".done").read_text(), "0\n")
        self.assertEqual(self.invoke("--collect", RUN)[:2], (0, "The answer\n"))

    def test_worker_crash_is_collected_as_finished(self):
        self.configure({})
        root = self.worker_files()

        def crash(argv, **kwargs):
            kwargs["stderr"].write(b"process crashed\n")
            return subprocess.CompletedProcess(argv, 17)

        self.process.side_effect = crash
        self.assertEqual(self.invoke("--worker", RUN)[0], 17)
        self.assertEqual(root.with_suffix(".done").read_text(), "17\n")
        self.assertEqual(self.invoke("--collect", RUN), (3, "", f"remote test: run {RUN} finished with exit 17 and no JSON result\nprocess crashed\n"))

    def test_worker_bad_cwd_records_completion(self):
        self.configure({"cwd": str(self.home / "missing")})
        root = self.worker_files()
        self.process.side_effect = FileNotFoundError("invalid working directory")
        self.assertEqual(self.invoke("--worker", RUN)[0], 2)
        self.assertEqual(self.process.call_args.kwargs["cwd"], str(self.home / "missing"))
        self.assertEqual(root.with_suffix(".done").read_text(), "2\n")
        code, _, stderr = self.invoke("--collect", RUN)
        self.assertEqual(code, 3)
        self.assertIn("finished with exit 2", stderr)
        self.assertIn("invalid working directory", stderr)

    def test_worker_missing_executable_records_completion(self):
        self.configure({})
        root = self.worker_files()
        self.which.return_value = None
        self.assertEqual(self.invoke("--worker", RUN)[0], 2)
        self.assertEqual(root.with_suffix(".done").read_text(), "2\n")
        self.assertIn("claude not found on PATH", root.with_suffix(".err").read_text())
        self.process.assert_not_called()
        self.assertEqual(self.invoke("--collect", RUN)[0], 3)

    def test_worker_hidden_and_run_validated(self):
        self.assertNotIn("--worker", self.invoke("--help")[1])
        self.assertEqual(self.invoke("--worker", "../escape")[0], 2)
        self.process.assert_not_called()

    def test_collect_running(self):
        self.seed_sessions()
        self.process.return_value.stdout = ("\n\x1e\n" + "\n".join(f"line {i}" for i in range(25)) + "\n\x1e\n").encode()
        code, stdout, stderr = self.invoke("--collect")
        self.assertEqual((code, stdout), (4, ""))
        self.assertEqual(stderr.splitlines(), [f"remote test: run {RUN} still running"] + [f"line {i}" for i in range(5, 25)])
        self.assertEqual(self.process.call_args.args[0][-1],
                         f'cat ~/.claude/local/remote-runs/{RUN}.json; printf "\\n\\036\\n"; cat ~/.claude/local/remote-runs/{RUN}.err; printf "\\n\\036\\n"; cat ~/.claude/local/remote-runs/{RUN}.done 2>/dev/null')

    def test_collect_ssh_finished_without_json(self):
        self.seed_sessions()
        for code in (0, 2, 127):
            with self.subTest(code=code):
                self.process.return_value.stdout = ("not json\n\x1e\n" + "\n".join(f"line {i}" for i in range(25)) + f"\n\x1e\n{code}\n").encode()
                result, stdout, stderr = self.invoke("--collect")
                self.assertEqual((result, stdout), (3, ""))
                self.assertEqual(stderr.splitlines(), [f"remote test: run {RUN} finished with exit {code} and no JSON result"] + [f"line {i}" for i in range(5, 25)])

    def test_collect_finished(self):
        self.seed_sessions()
        self.process.return_value.stdout = json.dumps(RESULT).encode() + b"\n\x1e\nlauncher message\n\x1e\n0\n"
        self.assertEqual(self.invoke("--collect")[:2], (0, "The answer\n"))
        record = json.loads(self.sessions.read_text())["machines"]["test"]
        self.assertEqual(record["session_id"], "test-session")
        self.assertEqual(record["run"], RUN)
        self.assertEqual(record["cwd"], "~/previous")
        self.which.assert_not_called()

    def test_collect_explicit_and_invalid_run(self):
        self.process.return_value.stdout = json.dumps(RESULT).encode() + b"\n\x1e\n\n\x1e\n"
        self.assertEqual(self.invoke("--collect", RUN)[0], 0)
        self.assertIn(RUN, self.process.call_args.args[0][-1])
        self.assertEqual(self.invoke("--collect", "../escape")[0], 2)

    def test_collect_without_record(self):
        code, _, stderr = self.invoke("--collect")
        self.assertEqual(code, 2)
        self.assertIn("test: no detached run recorded", stderr)

    def test_collect_ssh_failure(self):
        self.process.return_value = subprocess.CompletedProcess([], 255, b"", b"offline\n")
        self.assertEqual(self.invoke("--collect", RUN), (255, "", "offline\n"))

    def test_local_collect(self):
        self.configure({})
        root = self.home / ".claude/local/remote-runs"
        root.mkdir(parents=True)
        self.assertEqual(self.invoke("--collect", RUN)[0], 4)
        (root / (RUN + ".json")).write_text(json.dumps(RESULT), encoding="utf-8")
        (root / (RUN + ".err")).write_text("launcher", encoding="utf-8")
        self.assertEqual(self.invoke("--collect", RUN)[:2], (0, "The answer\n"))
        self.process.assert_not_called()
        self.which.assert_not_called()

    def test_runs(self):
        self.seed_sessions()
        self.process.return_value.stdout = b"one.json\none.err\n"
        self.assertEqual(self.invoke("--runs")[:2], (0, RUN + " 123\none.json\none.err\n"))
        self.assertEqual(self.process.call_args.args[0][-1], "ls -1 ~/.claude/local/remote-runs/")
        self.configure({})
        self.assertEqual(self.invoke("--runs")[:2], (0, RUN + " 123\n"))

    def test_local_runs_lists_sorted_files(self):
        self.configure({})
        self.seed_sessions()
        root = self.home / ".claude/local/remote-runs"
        root.mkdir(parents=True)
        names = ["z.json", "a.prompt", "a.done", "z.err"]
        for name in names:
            (root / name).touch()
        self.assertEqual(self.invoke("--runs")[:2], (0, RUN + " 123\n" + "\n".join(sorted(names)) + "\n"))
        self.process.assert_not_called()

    def test_validation_errors(self):
        cases = [
            (None, "remote must be an object"),
            ({"Bad": {}}, "invalid remote name"),
            ({"a" * 33: {}}, "invalid remote name"),
            ({"test": {"unknown": 1}}, "unknown keys: unknown"),
            ({"test": []}, "must be an object"),
            ({"test": {"shell": "zsh -ic"}}, "requires ssh"),
            ({"test": {"ssh": " "}}, "non empty string"),
            ({"test": {"cwd": None}}, "non empty string"),
            ({"test": {"permission_mode": []}}, "permission_mode"),
            ({"test": {"max_turns": True}}, "positive integer"),
            ({"test": {"timeout": 0}}, "positive integer"),
        ]
        for block, message in cases:
            with self.subTest(block=block):
                self.machine.write_text(json.dumps({"remote": block}), encoding="utf-8")
                code, _, stderr = self.invoke("hello")
                self.assertEqual(code, 2)
                self.assertTrue(stderr.startswith("remote: "))
                self.assertIn(message, stderr)
                with self.assertRaises(ValueError):
                    install.validate_remote({"remote": block}, lambda key: "source.json")

    def test_unknown_machine(self):
        self.machine.write_text('{"remote": {}}')
        self.assertEqual(self.invoke("hello"), (2, "", "remote: unknown machine test\n"))

    def test_dry_run(self):
        code, stdout, stderr = self.invoke("hello", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(shlex.split(stdout.splitlines()[0]), [
            "ssh", "-o", "BatchMode=yes", "test-alias", "cd ~/work/repo && claude -p --output-format json --model claude-opus-5-5" + SYSTEM_SUFFIX,
        ])
        self.assertIn("prompt length: 5", stdout)
        self.assertNotIn("hello", stdout)
        self.process.assert_not_called()
        self.assertFalse(self.sessions.exists())

    def test_dry_run_detach_does_not_write(self):
        self.configure({})
        with mock.patch.object(remote.subprocess, "Popen") as popen:
            self.assertEqual(self.invoke("hello", "--detach", "--dry-run")[0], 0)
            popen.assert_not_called()
        self.assertFalse((self.home / ".claude").exists())


class RemoteSkillsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.repo = Path(__file__).resolve().parents[1]
        self.machine = {"remote": {"test-one": {"ssh": "test-alias"}, "test-two": {}}}

    def test_generate_skills_and_preserve_removed_names(self):
        installer = install.Installer(self.home, self.repo, False)
        with contextlib.redirect_stdout(io.StringIO()):
            install.install_remote_skills(installer, self.machine, self.repo)
        for name in self.machine["remote"]:
            content = (self.home / ".claude/skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(install.REMOTE_SKILL_MARKER, content)
            self.assertIn((self.repo / "tools/remote_prompt.py").as_posix(), content)
            self.assertIn(f"{name} --prompt-file", content)
            description = content.splitlines()[2].removeprefix("description: ")
            self.assertIn(f'"on {name}:"', json.loads(description))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            install.install_remote_skills(installer, {"remote": {"test-one": {}}}, self.repo)
        self.assertIn("unchanged", output.getvalue())
        self.assertTrue((self.home / ".claude/skills/test-two/SKILL.md").is_file())

    def test_generation_dry_run(self):
        installer = install.Installer(self.home, self.repo, True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            install.install_remote_skills(installer, self.machine, self.repo)
        for name in self.machine["remote"]:
            self.assertIn(str(self.home / ".claude/skills" / name / "SKILL.md") + "  (would write)", output.getvalue())
        self.assertFalse((self.home / ".claude").exists())

    def test_conflict_refused_before_any_write(self):
        target = self.home / ".claude/skills/test-two/SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("user skill", encoding="utf-8")
        for dry_run in (False, True):
            installer = install.Installer(self.home, self.repo, dry_run)
            with mock.patch.object(installer, "write_bytes") as write:
                with self.assertRaisesRegex(ValueError, "test-two.*exists and was not written"):
                    install.check_remote_skills(self.machine, self.home, self.repo)
                    install.install_remote_skills(installer, self.machine, self.repo)
                write.assert_not_called()
        self.assertEqual(target.read_text(), "user skill")

    def test_installer_conflict_preflight(self):
        target = self.home / ".claude/skills/test-two/SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("user skill", encoding="utf-8")

        def load(repo, name, home, sources):
            sources["remote"] = "test configuration"
            return self.machine

        machine_name = sorted(path.stem for path in (self.repo / "machines").glob("*.json") if not path.name.endswith(".local.json"))[0]
        options = install.parse_args(["--machine", machine_name, "--home", str(self.home), "--dry-run", "--no-tests"])
        with mock.patch.object(install, "load_machine", side_effect=load), mock.patch.object(install.Installer, "write_bytes") as write, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(install.install(options), 2)
        self.assertIn(str(target), output.getvalue())
        write.assert_not_called()

    def test_repository_skill_name_refused(self):
        machine = {"remote": {"remote": {}}}
        install.validate_remote(machine, lambda key: "source.json")
        with self.assertRaisesRegex(ValueError, "remote.remote conflicts with a repository skill"):
            install.check_remote_skills(machine, self.home, self.repo)

    def test_skill_conflicts_use_selected_repository(self):
        repo = self.home / "selected-repo"
        (repo / "claude/skills/test-one").mkdir(parents=True)
        install.validate_remote(self.machine, lambda key: "source.json")
        with self.assertRaisesRegex(ValueError, "remote.test-one conflicts with a repository skill"):
            install.check_remote_skills(self.machine, self.home, repo)
        install.check_remote_skills({"remote": {"remote": {}}}, self.home, repo)

    def test_generation_does_not_repeat_preflight(self):
        installer = install.Installer(self.home, self.repo, True)
        install.validate_remote(self.machine, lambda key: "source.json")
        install.check_remote_skills(self.machine, self.home, self.repo)
        with mock.patch.object(install, "validate_remote") as validate, mock.patch.object(install, "check_remote_skills") as check, mock.patch.object(installer, "write_bytes") as write:
            install.install_remote_skills(installer, self.machine, self.repo)
        validate.assert_not_called()
        check.assert_not_called()
        self.assertEqual(write.call_count, 2)

    def test_remote_guidance_covers_git_bash_and_posix_targets(self):
        installer = install.Installer(self.home, self.repo, True)
        with mock.patch.object(installer, "write_bytes") as write:
            install.install_remote_skills(installer, self.machine, self.repo)
        texts = [call.args[1].decode() for call in write.call_args_list]
        texts.append((self.repo / "claude/skills/remote/SKILL.md").read_text(encoding="utf-8"))
        if (self.repo / "README.md").is_file():
            texts.append((self.repo / "README.md").read_text(encoding="utf-8"))
        for text in texts:
            self.assertIn("MSYS_NO_PATHCONV=1", text)
            self.assertIn("quote the value", text)
            self.assertIn("An ssh entry must point at a machine whose login shell is POSIX; a Windows host over ssh is not supported.", text)

    def test_generation_quotes_tool_path(self):
        repo = self.home / "intermediate" / ".." / "repo with spaces"
        installer = install.Installer(self.home, repo, True)
        with mock.patch.object(installer, "write_bytes") as write:
            install.install_remote_skills(installer, {"remote": {"test": {}}}, repo)
        self.assertIn("python " + shlex.quote((repo / "tools/remote_prompt.py").resolve().as_posix()), write.call_args.args[1].decode())


if __name__ == "__main__":
    unittest.main()
