import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_run


HEADER = (
    "codex via the proxy pool\n"
    "Reading additional input from stdin...\n"
    "OpenAI Codex v0.164.0\n"
    + "-" * 8 + "\n"
    "workdir: /work/acme\n"
    "model: fixture-model\n"
    "provider: fixture-provider\n"
    "approval: never\n"
    "sandbox: workspace-write\n"
    "reasoning effort: high\n"
    "reasoning summaries: none\n"
    "session id: fixture-session\n"
    + "-" * 8 + "\n"
)


class CodexRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.machine = self.home / ".claude" / "local" / "machine.json"
        self.machine.parent.mkdir(parents=True)
        self.configure({"codex_model": "fixture-model"})
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.prompt = self.home / "brief.md"
        self.prompt.write_bytes(b"Write a report.\n")
        self.out = self.home / "reports" / "out.md"
        self.log = self.home / "logs" / "run.log"
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}))
        self.which = self.enterContext(mock.patch.object(codex_run.shutil, "which", return_value="/bin/codex.CMD"))
        self.popen = self.enterContext(mock.patch.object(codex_run.subprocess, "Popen", side_effect=AssertionError("real process forbidden")))
        self.run = self.enterContext(mock.patch.object(codex_run, "run_codex", side_effect=self.fake_run))
        self.child_code = 0
        self.child_log = HEADER + "user\nWrite a report.\ncodex\nFinished.\n"
        self.child_report = b"CHANGED FILES:\nVERIFIED:\nASSUMPTIONS/RISKS:\n"

    def configure(self, value):
        self.machine.write_text(json.dumps(value), encoding="utf-8-sig")

    def fake_run(self, command, prompt, log):
        self.assertEqual(prompt.mode, "rb")
        self.assertEqual(prompt.read(), self.prompt.read_bytes())
        self.assertFalse(self.out.exists(), "stale output must be removed before launch")
        self.assertTrue(self.out.parent.is_dir())
        self.assertTrue(self.log.parent.is_dir())
        log.write(self.child_log.encode("utf-8"))
        if self.child_report is not None:
            Path(command[command.index("--output-last-message") + 1]).write_bytes(self.child_report)
        return self.child_code

    def arguments(self, command="exec", selector=None):
        arguments = [command]
        if command == "exec":
            arguments.extend(["--repo", str(self.repo)])
        else:
            arguments.append(selector or "fixture-session")
        return arguments + ["--prompt", str(self.prompt), "--out", str(self.out), "--log", str(self.log)]

    def invoke(self, arguments=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = codex_run.main(self.arguments() if arguments is None else arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_usage(self, arguments):
        code, output, errors = self.invoke(arguments)
        self.assertEqual(code, 3)
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertTrue(errors.startswith("codex_run: "))
        self.assertEqual(output, "next: fix the arguments and run again\n")
        self.run.assert_not_called()

    def test_success_and_exec_command(self):
        code, output, errors = self.invoke(self.arguments() + ["--skip-git-repo-check"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output.splitlines(), [
            "codex_run: session fixture-session, model fixture-model, provider fixture-provider",
            f"done, report {self.out} (3 lines)",
            f"next: read {self.out}, then check CHANGED FILES against git diff --stat and rerun VERIFIED",
        ])
        self.assertEqual(self.run.call_args.args[0], [
            "/bin/codex.CMD", "exec", "-", "-C", str(self.repo), "-m", "fixture-model",
            "-c", 'model_reasoning_effort="high"', "--sandbox", "workspace-write",
            "--skip-git-repo-check", "--output-last-message", str(self.out),
        ])
        self.which.assert_called_once_with("codex")
        self.assertTrue(self.log.read_bytes().endswith(b"exit=0\n"))

    def test_missing_report_is_quota_with_session_resume(self):
        self.child_report = None
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (2, ""))
        self.assertIn("quota exhausted, reset unknown\n", output)
        self.assertIn(Path(codex_run.__file__).resolve().as_posix(), output.splitlines()[-1])
        self.assertIn("resume fixture-session --prompt", output.splitlines()[-1])
        self.assertIn("run-resume1.log", output.splitlines()[-1])

    def test_quota_resume_log_preserves_given_separators(self):
        self.log.parent.mkdir()
        self.log.write_text(HEADER, encoding="utf-8")
        cases = [
            ("../a/run.log", "../a/run-resume1.log"),
            (r"..\a\run.log", r"..\a\run-resume1.log"),
            ("../a/run-resume1.log", "../a/run-resume2.log"),
            (r"..\a\run-resume1.log", r"..\a\run-resume2.log"),
        ]
        for given, expected in cases:
            with self.subTest(log=given), contextlib.redirect_stdout(io.StringIO()) as output:
                args = codex_run.parse_args(self.arguments() + ["--log", given])
                self.assertEqual(codex_run.report(args, self.out, self.log, 0), 2)
                self.assertIn(expected, output.getvalue().splitlines()[-1])

    def test_empty_report_is_quota(self):
        self.child_report = b""
        self.assertEqual(self.invoke()[0], 2)

    def test_usage_limit_extracts_reset(self):
        self.child_code = 1
        self.child_log += "ERROR: You've hit your usage limit. Try again at 8:01 PM.\n"
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (2, ""))
        self.assertIn("quota exhausted, reset 8:01 PM\n", output)

    def test_recovered_quota_keeps_success_and_next_line_last(self):
        self.child_log = HEADER + (
            "user\nWrite a report.\ncodex\n"
            "stream error: HTTP status 429, retrying\nFinished.\n"
        )
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(f"done, report {self.out}", output)
        self.assertEqual(output.splitlines()[-2], "note: a quota or rate limit line appeared during the run; codex recovered")
        self.assertTrue(output.splitlines()[-1].startswith("next: read "))

    def test_recovered_quota_still_checks_model(self):
        self.child_log = self.child_log.replace("model: fixture-model", "model: other-model")
        self.child_log += "stream error: status 429, retrying\nFinished.\n"
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (1, ""))
        self.assertIn("wrong model: asked fixture-model, got other-model", output)
        self.assertIn("codex recovered\nnext: read", output)

    def test_retry_and_numeric_identifiers_are_not_quota(self):
        self.child_code = 1
        self.child_report = None
        for line in ("ERROR connection lost, try again", "ERROR status 14290", "ERROR request id 429 failed"):
            with self.subTest(line=line):
                self.child_log = HEADER + "user\nWrite a report.\ncodex\n" + line
                self.assertEqual(self.invoke()[0], 1)

    def test_prompt_quota_and_header_spoof_do_not_trigger(self):
        for marker in ("codex", "exec", "thinking"):
            with self.subTest(marker=marker):
                self.child_log = HEADER + (
                    "user\nERROR: quota, try again at midnight.\n"
                    "model: other-model\nsession id: other-session\nprovider: other-provider\n"
                    + "-" * 8 + "\n" + marker + "\nFinished.\n"
                )
                code, output, errors = self.invoke()
                self.assertEqual((code, errors), (0, ""))
                self.assertIn("session fixture-session, model fixture-model, provider fixture-provider", output)

    def test_rmcp_noise_does_not_trigger(self):
        self.child_log += "2026-11-14T14:40:52.005758Z ERROR rmcp::transport::worker: quota 429 try again\n"
        self.assertEqual(self.invoke()[0], 0)

    def test_nonzero_without_quota_fails(self):
        self.child_code = 7
        self.child_report = None
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (1, ""))
        self.assertIn(f"failed, codex exit 7, see {self.log}\n", output)
        self.assertEqual(output.splitlines()[-1], f"next: read the last 40 lines of {self.log}")
        self.assertTrue(self.log.read_bytes().endswith(b"exit=7\n"))

    def test_quota_takes_precedence_over_nonzero_and_wrong_model(self):
        self.child_code = 7
        self.child_log = self.child_log.replace("model: fixture-model", "model: other-model")
        self.child_log += "ERROR quota\n"
        self.assertEqual(self.invoke()[0], 2)

    def test_wrong_model_keeps_report(self):
        self.child_log = self.child_log.replace("model: fixture-model", "model: other-model")
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (1, ""))
        self.assertIn("wrong model: asked fixture-model, got other-model\n", output)
        self.assertEqual(self.out.read_bytes(), self.child_report)

    def test_model_comparison_ignores_case(self):
        self.child_log = self.child_log.replace("model: fixture-model", "model: FIXTURE-MODEL")
        self.assertEqual(self.invoke()[0], 0)

    def test_stale_report_preserved_and_log_truncated(self):
        self.out.parent.mkdir()
        self.out.write_text("old report", encoding="utf-8")
        previous = self.out.with_name("out.prev.md")
        previous.write_text("older report", encoding="utf-8")
        self.log.parent.mkdir()
        self.log.write_text("ERROR quota\n", encoding="utf-8")
        self.child_report = None
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (2, ""))
        self.assertFalse(self.out.exists())
        self.assertEqual(previous.read_text(encoding="utf-8"), "old report")
        self.assertEqual(output.splitlines()[-2], f"previous report kept at {previous}")
        self.assertNotIn("ERROR quota", self.log.read_text(encoding="utf-8"))

    def test_previous_report_is_quiet_on_success(self):
        self.out.parent.mkdir()
        self.out.write_text("old report", encoding="utf-8")
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(self.out.with_name("out.prev.md").read_text(encoding="utf-8"), "old report")
        self.assertEqual(self.out.read_bytes(), self.child_report)
        self.assertNotIn("previous report", output)

    def test_previous_report_named_on_failure_and_wrong_model(self):
        self.out.parent.mkdir()
        for child_code in (1, 0):
            with self.subTest(child_code=child_code):
                self.out.write_text("old report", encoding="utf-8")
                self.child_code = child_code
                self.child_log = HEADER.replace("model: fixture-model", "model: other-model")
                code, output, errors = self.invoke()
                self.assertEqual((code, errors), (1, ""))
                previous = self.out.with_name("out.prev.md")
                self.assertEqual(previous.read_text(encoding="utf-8"), "old report")
                self.assertEqual(output.splitlines()[-2], f"previous report kept at {previous}")

    def test_previous_report_cannot_overwrite_prompt_or_log(self):
        self.out.parent.mkdir()
        self.out.write_text("old report", encoding="utf-8")
        previous = self.out.with_name("out.prev.md")
        self.assert_usage(self.arguments() + ["--log", str(previous)])
        previous.write_text("prompt", encoding="utf-8")
        self.assert_usage(self.arguments() + ["--prompt", str(previous)])
        self.assertEqual(self.out.read_text(encoding="utf-8"), "old report")
        self.assertEqual(previous.read_text(encoding="utf-8"), "prompt")

    def test_resume_command_and_last(self):
        for selector in ("fixture-session", "--last"):
            with self.subTest(selector=selector):
                code, output, errors = self.invoke(self.arguments("resume", selector) + ["--sandbox", "read-only"])
                self.assertEqual((code, errors), (0, ""))
                command = self.run.call_args.args[0]
                self.assertEqual(command, [
                    "/bin/codex.CMD", "exec", "resume", selector, "-m", "fixture-model",
                    "-c", 'model_reasoning_effort="high"', "-c", 'sandbox_mode="read-only"',
                    "--output-last-message", str(self.out), "-",
                ])
                self.assertNotIn("--sandbox", command)

    def test_effort_null_omits_override(self):
        self.configure({"codex_model": "fixture-model", "codex_effort": None})
        self.assertEqual(self.invoke()[0], 0)
        self.assertNotIn("-c", self.run.call_args.args[0])

    def test_explicit_settings_override_machine(self):
        self.configure({"codex_model": "other-model", "codex_effort": None})
        self.assertEqual(self.invoke(self.arguments() + ["--model", "fixture-model", "--effort", "low", "--sandbox", "read-only"])[0], 0)
        command = self.run.call_args.args[0]
        self.assertEqual(command[command.index("-m") + 1], "fixture-model")
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

    def test_quota_resume_carries_only_explicit_overrides(self):
        self.child_report = None
        cases = [
            [],
            ["--model", "fixture-model"],
            ["--effort", "low"],
            ["--sandbox", "workspace-write"],
            ["--model=fixture-model", "--effort=low", "--sandbox=read-only"],
        ]
        for command in ("exec", "resume"):
            for options in cases:
                with self.subTest(command=command, options=options):
                    code, output, errors = self.invoke(self.arguments(command) + options)
                    self.assertEqual((code, errors), (2, ""))
                    next_line = output.splitlines()[-1]
                    for option in ("--model", "--effort", "--sandbox"):
                        explicit = any(value == option or value.startswith(option + "=") for value in options)
                        self.assertEqual(option in next_line, explicit)
                    if options and "=" in options[0]:
                        self.assertTrue(next_line.endswith("--model fixture-model --effort low --sandbox read-only"))
                    elif options:
                        self.assertTrue(next_line.endswith(" ".join(options)))

    def test_home_default_and_missing_machine(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.home):
            self.assertEqual(self.invoke()[0], 0)
        self.machine.unlink()
        self.assertEqual(self.invoke(self.arguments() + ["--model", "fixture-model"])[0], 0)
        self.assertIn('model_reasoning_effort="high"', self.run.call_args.args[0])

    def test_unknown_header_and_last_resume(self):
        self.child_log = "ERROR quota\n"
        self.child_report = None
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (2, ""))
        self.assertEqual(output.splitlines()[0], "codex_run: session unknown, model unknown, provider unknown")
        self.assertEqual(output.splitlines()[-2], "session id not found in the log; --last resumes the newest session in this folder, check it is this one")
        self.assertIn("resume --last --prompt", output.splitlines()[-1])

    def test_missing_model_header_succeeds_with_note(self):
        self.child_log = self.child_log.replace("model: fixture-model\n", "")
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output.splitlines()[-2], "note: the log carries no model line; model not confirmed")
        self.assertTrue(output.splitlines()[-1].startswith("next: read "))

    def test_exact_prompt_with_markers_is_removed_before_quota_scan(self):
        prompt = "Review this example:\ncodex\nERROR quota\nexec\nERROR status 429\nthinking\nDone."
        self.prompt.write_bytes((prompt.replace("\n", "\r\n") + "\r\n \t").encode("utf-8"))
        self.child_log = HEADER + "user\n" + prompt + "\nERROR connection lost\n"
        self.child_code = 1
        self.child_report = None
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (1, ""))
        self.assertNotIn("quota exhausted", output)

    def test_quota_after_exact_prompt_without_marker_is_detected(self):
        self.child_log = HEADER + "user\nWrite a report.\nERROR rate limit, try again in 1 hour.\n"
        self.child_code = 1
        self.child_report = None
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (2, ""))
        self.assertIn("quota exhausted, reset 1 hour", output)

    def test_altered_prompt_falls_back_to_markers(self):
        self.prompt.write_text("Original prompt with ERROR quota example.", encoding="utf-8")
        self.child_log = HEADER + "user\nAltered prompt\nERROR quota example\ncodex\nERROR connection lost\n"
        self.child_code = 1
        self.child_report = None
        self.assertEqual(self.invoke()[0], 1)
        self.child_log += "ERROR quota\n"
        self.assertEqual(self.invoke()[0], 2)

    def test_missing_or_empty_prompt(self):
        self.prompt.unlink()
        self.assert_usage(self.arguments())
        self.prompt.write_bytes(b"")
        self.assert_usage(self.arguments())

    def test_bad_repo(self):
        self.assert_usage(self.arguments() + ["--repo", str(self.home / "missing")])

    def test_unresolved_model(self):
        self.configure({})
        self.assert_usage(self.arguments())

    def test_parent_creation_failure(self):
        for path in (self.out.parent, self.log.parent):
            with self.subTest(path=path):
                path.write_text("file blocks directory", encoding="utf-8")
                self.assert_usage(self.arguments())
                path.unlink()

    def test_conflicting_paths_rejected_before_truncation(self):
        for option, path in (("--out", self.prompt), ("--log", self.prompt), ("--out", self.log)):
            with self.subTest(option=option, path=path):
                self.assert_usage(self.arguments() + [option, str(path)])
                self.assertEqual(self.prompt.read_bytes(), b"Write a report.\n")

    def test_bad_arguments_and_resume_selector(self):
        for arguments in ([], ["exec"], self.arguments("resume")[0:1] + self.arguments()[3:], self.arguments("resume") + ["--last"]):
            with self.subTest(arguments=arguments):
                self.assert_usage(arguments)

    def test_codex_missing(self):
        self.which.return_value = None
        self.assertEqual(self.invoke(), (127, "next: install codex or fix PATH\n", "codex_run: codex not found on PATH\n"))
        self.run.assert_not_called()

    def test_codex_disappears_before_launch(self):
        self.run.side_effect = FileNotFoundError()
        self.assertEqual(self.invoke(), (127, "next: install codex or fix PATH\n", "codex_run: codex not found on PATH\n"))

    def test_interrupt_logs_exit_without_traceback(self):
        self.run.side_effect = KeyboardInterrupt()
        self.assertEqual(self.invoke(), (130, "", ""))
        self.assertTrue(self.log.read_bytes().endswith(b"exit=130\n"))

    def test_log_exit_is_separate_line(self):
        self.child_log = self.child_log.rstrip("\n")
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.log.read_text(encoding="utf-8").splitlines()[-1], "exit=0")

    def test_launch_error_is_failure(self):
        self.run.side_effect = PermissionError("launch denied")
        code, output, errors = self.invoke()
        self.assertEqual((code, errors), (1, ""))
        self.assertIn("failed, codex exit 1", output)
        self.assertTrue(self.log.read_bytes().endswith(b"exit=1\n"))

    def test_relative_output_resolves_outside_child_repo(self):
        with mock.patch.object(codex_run.Path, "resolve", autospec=True, side_effect=lambda path: self.home / path):
            arguments = self.arguments() + ["--out", "reports/out.md", "--log", "logs/run.log"]
            self.assertEqual(self.invoke(arguments)[0], 0)
        self.assertEqual(self.run.call_args.args[0][-1], str(self.out))


class ParsingTests(unittest.TestCase):
    def test_qualifying_quota_lines_and_resets(self):
        cases = [
            ("ERROR: usage limit. Try again at 8:01 PM.", "8:01 PM"),
            ("error rate limit, try again in 2 hours. More text.", "2 hours"),
            ("2026-11-14T14:40:52.005758Z ERROR quota, resets at midnight.", "midnight"),
            ("2026-11-14 14:40:52+00:00 error status 429 reset at 20:01 UTC", "20:01 UTC"),
            ("stream error: insufficient_quota", "unknown"),
            ("Stream Error: 429, retrying", "unknown"),
            ("ERROR HTTP/1.1 429 Too Many Requests", "unknown"),
            ("ERROR 429: Too Many Requests", "unknown"),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                header, quota, reset = codex_run.read_run(HEADER + "user\nprompt\ncodex\n" + line)
                self.assertTrue(quota)
                self.assertEqual(reset, expected)

    def test_non_error_mentions_ignored(self):
        for line in (
            "quota 429 try again", "ERROR connection lost", "INFO quota", "ERROR rmcp::worker quota",
            "ERROR try again later", "stream error: try again later", "ERROR status 14290",
            "ERROR status code=4290", "ERROR request id=429", "ERROR status X429",
        ):
            with self.subTest(line=line):
                self.assertFalse(codex_run.read_run(HEADER + "user\nprompt\ncodex\n" + line)[1])

    def test_exact_prompt_removed_only_once_with_normalized_line_endings(self):
        prompt = "Example:\r\ncodex\r\nERROR quota\r\n \t"
        echo = "Example:\ncodex\nERROR quota"
        text = HEADER + "user\n" + echo + "\nERROR connection lost\n"
        self.assertFalse(codex_run.read_run(text, prompt)[1])
        self.assertTrue(codex_run.read_run(text + echo, prompt)[1])

    def test_launcher_optional_and_missing_fields(self):
        header = codex_run.read_run("\n".join(HEADER.splitlines()[2:]))[0]
        self.assertEqual(header["session id"], "fixture-session")
        self.assertEqual(codex_run.read_run("no header"), ({}, False, "unknown"))

    def test_resume_log_number_skips_existing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run-resume2.log").touch()
            self.assertEqual(codex_run.resume_log(root / "run-resume1.log"), str(root / "run-resume3.log"))

    def test_help_lists_all_exit_codes(self):
        for arguments in (["--help"], ["exec", "--help"], ["resume", "--help"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    codex_run.main(arguments)
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(codex_run.EXIT_HELP, " ".join(output.getvalue().split()))


class ChildProcessTests(unittest.TestCase):
    def test_stdin_and_combined_log(self):
        child = mock.Mock()
        child.wait.return_value = 7
        prompt, log = object(), object()
        with mock.patch.object(codex_run.subprocess, "Popen", return_value=child) as popen:
            self.assertEqual(codex_run.run_codex(["fixture-codex", "exec"], prompt, log), 7)
        popen.assert_called_once_with(["fixture-codex", "exec"], stdin=prompt, stdout=log, stderr=subprocess.STDOUT)

    def test_interrupt_terminates_and_waits(self):
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        with mock.patch.object(codex_run.subprocess, "Popen", return_value=child):
            with self.assertRaises(KeyboardInterrupt):
                codex_run.run_codex(["fixture-codex"], object(), object())
        child.terminate.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [mock.call(), mock.call(timeout=5)])

    def test_interrupt_kills_unresponsive_child(self):
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("fixture-codex", 5), 0]
        with mock.patch.object(codex_run.subprocess, "Popen", return_value=child):
            with self.assertRaises(KeyboardInterrupt):
                codex_run.run_codex(["fixture-codex"], object(), object())
        child.kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
