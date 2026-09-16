import contextlib
import datetime
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import agy_llm
import pr_review_runner as runner
import pr_review_score as scorer


FINDINGS = [
    {
        "file": "app.py",
        "line": 12,
        "severity": "high",
        "claim": "The changed branch can return the wrong value.",
        "evidence": "+    return wrong_value",
        "confidence": 0.9,
    },
    {
        "file": "util.py",
        "line": 40,
        "severity": "medium",
        "claim": "The helper does not handle an empty input.",
        "evidence": "+    return values[0]",
        "confidence": 0.8,
    },
]


class PRReviewTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.diff = self.root / "change.patch"
        self.diff.write_text(
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -11,1 +11,2 @@\n"
            "+    return wrong_value\n"
            "diff --git a/util.py b/util.py\n"
            "--- a/util.py\n"
            "+++ b/util.py\n"
            "@@ -39,1 +39,2 @@\n"
            "+    return values[0]\n",
            encoding="utf-8",
        )
        plain_output = json.dumps(FINDINGS)
        wrapped_output = json.dumps(
            {
                "response": f"```json\n{plain_output}\n```",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            }
        )
        self.plain = self.write_fake("plain.py", f"print({plain_output!r})\n")
        self.wrapped = self.write_fake("wrapped.py", f"print({wrapped_output!r})\n")
        self.failed = self.write_fake(
            "failed.py",
            "import sys\n"
            "print('reviewer failure\\nwith details', file=sys.stderr)\n"
            "raise SystemExit(2)\n",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_fake(self, name, source):
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return path

    def write_fake_agy(
        self,
        name,
        output_lines=None,
        delay=0,
        capture_path=None,
        runtime_path=None,
        exit_code=0,
    ):
        script = self.root / f"{name}.py"
        source = textwrap.dedent(
            f"""
            import json
            import os
            import sys
            import time

            expected = [
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--model", "test-model",
                "--sandbox",
                "-p=",
            ]
            event = json.loads(sys.stdin.readline())
            valid = (
                sys.argv[1:] == expected
                and event.get("event") == "user"
                and isinstance(event.get("message", {{}}).get("content"), str)
            )
            if not valid:
                print(json.dumps({{
                    "event": "result",
                    "result": {{"status": "ERROR", "error": "bad invocation"}},
                }}))
                raise SystemExit(1)
            if {capture_path is not None!r}:
                with open({str(capture_path)!r}, "w", encoding="utf-8") as handle:
                    handle.write(event["message"]["content"])
            if {runtime_path is not None!r}:
                with open({str(runtime_path)!r}, "w", encoding="utf-8") as handle:
                    json.dump({{
                        "cwd": os.getcwd(),
                        "entries": os.listdir(),
                        "arguments": sys.argv[1:],
                    }}, handle)
            time.sleep({delay!r})
            for line in {list(output_lines or [])!r}:
                print(line)
            raise SystemExit({exit_code!r})
            """
        ).lstrip()
        script.write_text(source, encoding="utf-8")
        if os.name == "nt":
            launcher = self.root / f"{name}.cmd"
            command = subprocess.list2cmdline([sys.executable, str(script)])
            launcher.write_text(f"@echo off\n{command} %*\n", encoding="utf-8")
        else:
            launcher = self.root / f"{name}.sh"
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            launcher.write_text(f"#!/bin/sh\nexec {command} \"$@\"\n", encoding="utf-8")
            launcher.chmod(0o755)
        return launcher

    def command(self, path):
        executable = Path(sys.executable)
        if not executable.is_file():
            executable = Path(sys.prefix).parent / "python.exe"
        return subprocess.list2cmdline([str(executable), str(path)])

    def run_main(self, function, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = function(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_agy(self, fake, prompt="Review this: café", *arguments):
        stdin = io.StringIO(prompt)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = agy_llm.main(
                    ["--agy", str(fake), "--model", "test-model", *arguments]
                )
        return code, stdout.getvalue(), stderr.getvalue()

    def reviewer_arguments(self, include_failure=True):
        arguments = [
            "--reviewer",
            f"plain={self.command(self.plain)}",
            "--reviewer",
            f"wrapped={self.command(self.wrapped)}",
        ]
        if include_failure:
            arguments.extend(["--reviewer", f"failed={self.command(self.failed)}"])
        return arguments

    def test_runner_dedupes_wrapped_and_plain_findings(self):
        output = self.root / "findings.json"
        arguments = [
            "--repo",
            str(self.root),
            "--base",
            "main",
            "--diff",
            str(self.diff),
            "--reviewers",
            "plain,wrapped,failed",
            "--out",
            str(output),
            *self.reviewer_arguments(),
        ]
        code, stdout, stderr = self.run_main(runner.main, arguments)
        self.assertEqual(code, 0, stderr)
        self.assertIn("failed", stdout)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(document["findings"]), 2)
        for finding in document["findings"]:
            self.assertEqual(finding["reviewers"], ["plain", "wrapped"])
        statuses = {item["name"]: item for item in document["reviewers"]}
        self.assertEqual(statuses["plain"]["status"], "ok")
        self.assertEqual(statuses["wrapped"]["usage"]["input_tokens"], 100)
        self.assertEqual(statuses["failed"]["status"], "error")
        self.assertIn("reviewer failure with details", statuses["failed"]["error"])

    def test_read_diff_falls_back_after_empty_branch_diff(self):
        commands = (
            ["git", "diff", "main...HEAD"],
            ["git", "merge-base", "main", "HEAD"],
            ["git", "diff", "main"],
        )
        diff = self.diff.read_text(encoding="utf-8")
        for empty in ("", " \n\t"):
            with self.subTest(stdout=empty):
                results = [
                    subprocess.CompletedProcess(commands[0], 0, stdout=empty, stderr=""),
                    subprocess.CompletedProcess(commands[1], 1, stdout="", stderr="no merge base"),
                    subprocess.CompletedProcess(commands[2], 0, stdout=diff, stderr=""),
                ]
                with mock.patch.object(runner.subprocess, "run", side_effect=results) as run:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(runner.read_diff(self.root, "main"), diff)
                self.assertEqual([call.args[0] for call in run.call_args_list], list(commands))

    def test_read_diff_returns_empty_when_both_attempts_are_empty(self):
        results = [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=" \n\t", stderr=""),
        ]
        with mock.patch.object(runner.subprocess, "run", side_effect=results) as run:
            self.assertEqual(runner.read_diff(self.root, "main"), "")
        self.assertEqual(run.call_count, 3)

    def test_read_diff_returns_empty_after_failed_first_attempt(self):
        results = [
            subprocess.CompletedProcess([], 1, stdout="", stderr="comparison failed"),
            subprocess.CompletedProcess([], 1, stdout="", stderr="no merge base"),
            subprocess.CompletedProcess([], 0, stdout=" \n\t", stderr=""),
        ]
        with mock.patch.object(runner.subprocess, "run", side_effect=results) as run:
            self.assertEqual(runner.read_diff(self.root, "main"), "")
        self.assertEqual(run.call_count, 3)

    def test_read_diff_reports_selected_command_on_stderr(self):
        diff = self.diff.read_text(encoding="utf-8")
        for selected in ("main...HEAD", "main"):
            with self.subTest(selected=selected):
                results = []
                if selected == "main":
                    results.append(subprocess.CompletedProcess([], 0, stdout="", stderr=""))
                    results.append(subprocess.CompletedProcess([], 1, stdout="", stderr="no merge base"))
                results.append(subprocess.CompletedProcess([], 0, stdout=diff, stderr=""))
                stderr = io.StringIO()
                with mock.patch.object(runner.subprocess, "run", side_effect=results):
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(runner.read_diff(self.root, "main"), diff)
                self.assertEqual(stderr.getvalue(), f"pr-review: diff from git diff {selected}\n")

    def test_read_diff_uses_merge_base_for_working_tree(self):
        sha = "a" * 40
        commands = [
            ["git", "diff", "main...HEAD"],
            ["git", "merge-base", "main", "HEAD"],
            ["git", "diff", sha],
        ]
        diff = self.diff.read_text(encoding="utf-8")
        results = [
            subprocess.CompletedProcess(commands[0], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(commands[1], 0, stdout=sha + "\n", stderr=""),
            subprocess.CompletedProcess(commands[2], 0, stdout=diff, stderr=""),
        ]
        stderr = io.StringIO()
        with mock.patch.object(runner.subprocess, "run", side_effect=results) as run:
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(runner.read_diff(self.root, "main"), diff)
        self.assertEqual([call.args[0] for call in run.call_args_list], commands)
        self.assertTrue(all(call.kwargs["cwd"] == self.root for call in run.call_args_list))
        self.assertEqual(stderr.getvalue(), f"pr-review: diff from git diff {sha}\n")

    def test_read_diff_uses_base_when_merge_base_fails(self):
        commands = [
            ["git", "diff", "main...HEAD"],
            ["git", "merge-base", "main", "HEAD"],
            ["git", "diff", "main"],
        ]
        diff = self.diff.read_text(encoding="utf-8")
        results = [
            subprocess.CompletedProcess(commands[0], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(commands[1], 1, stdout="", stderr="no merge base"),
            subprocess.CompletedProcess(commands[2], 0, stdout=diff, stderr=""),
        ]
        stderr = io.StringIO()
        with mock.patch.object(runner.subprocess, "run", side_effect=results) as run:
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(runner.read_diff(self.root, "main"), diff)
        self.assertEqual([call.args[0] for call in run.call_args_list], commands)
        self.assertEqual(stderr.getvalue(), "pr-review: diff from git diff main\n")

    def test_empty_diff_exits_one(self):
        empty = self.root / "empty.patch"
        empty.write_text("\n", encoding="utf-8")
        code, _, stderr = self.run_main(
            runner.main,
            [
                "--repo",
                str(self.root),
                "--diff",
                str(empty),
                "--reviewers",
                "plain",
                *self.reviewer_arguments(include_failure=False),
            ],
        )
        self.assertEqual(code, 1)
        self.assertIn("diff is empty", stderr)

    def test_scorer_reports_recall_and_false_positives(self):
        cases = self.root / "cases"
        case = cases / "sample"
        case.mkdir(parents=True)
        (case / "diff.patch").write_text(self.diff.read_text(encoding="utf-8"), encoding="utf-8")
        (case / "expected.json").write_text(
            json.dumps(
                [
                    {
                        "id": "wrong-return",
                        "file": "app.py",
                        "lines": [10, 14],
                        "keywords": ["wrong value"],
                        "description": "The branch returns the wrong value.",
                    },
                    {
                        "id": "missing-file",
                        "file": "nothing.py",
                        "lines": [1, 2],
                        "keywords": ["unmentioned defect"],
                        "description": "A defect no fake reviewer reports.",
                    },
                ]
            ),
            encoding="utf-8",
        )
        arguments = [
            "--cases",
            str(cases),
            "--reviewers",
            "plain,wrapped",
            "--price",
            "wrapped=0.75,3.75",
            *self.reviewer_arguments(include_failure=False),
        ]
        code, _, stderr = self.run_main(scorer.main, arguments)
        self.assertEqual(code, 0, stderr)
        output = cases / f"scores-{datetime.date.today().isoformat()}.json"
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(document["cases"]), 2)
        for result in document["cases"]:
            self.assertEqual(result["recall"], 0.5)
            self.assertEqual(result["false_positives"], 1)
        wrapped = next(item for item in document["cases"] if item["reviewer"] == "wrapped")
        self.assertIsNotNone(wrapped["cost"])
        plain = next(item for item in document["cases"] if item["reviewer"] == "plain")
        self.assertIsNone(plain["cost"])

    def test_scorer_passes_resolved_repo_to_reviewers(self):
        cases = self.root / "repo-cases"
        case = cases / "sample"
        case.mkdir(parents=True)
        (case / "diff.patch").write_text("diff text", encoding="utf-8")
        (case / "expected.json").write_text("[]", encoding="utf-8")
        repo = self.root / "review repo"
        with mock.patch.object(scorer.runner, "run_reviewers", return_value=[]) as run:
            rows = scorer.score_cases(cases, [], scorer.DEFAULT_PRICES, repo=repo)
        self.assertEqual(rows, [])
        self.assertEqual(run.call_args.kwargs["repo"], repo.resolve())

    def test_subscription_reviewers_have_zero_cost(self):
        self.assertEqual(scorer.review_cost(None, scorer.DEFAULT_PRICES["agy"]), 0.0)
        self.assertEqual(scorer.review_cost(None, scorer.DEFAULT_PRICES["codex"]), 0.0)
        self.assertNotIn("gemini", scorer.DEFAULT_PRICES)

    def test_reviewer_override_replaces_default(self):
        override = runner.parse_override(f"agy={self.command(self.plain)}")
        configs = runner.load_reviewer_configs([override], home=self.home)
        self.assertEqual(configs["agy"]["cmd"], self.command(self.plain))
        self.assertTrue(configs["agy"]["stdin"])
        self.assertEqual(list(configs), ["agy", "codex", "kimi"])

    def test_prepare_command_replaces_repo_with_resolved_path(self):
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "codex")
        repo = self.root / "repo with spaces"
        command = runner.prepare_command(config, "prompt", repo=repo)
        self.assertEqual(command[command.index("-C") + 1], repo.resolve().as_posix())

    def test_prepare_command_uses_configured_codex_model(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(
            json.dumps({"codex_model": "test-model"}), encoding="utf-8"
        )
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "codex")
        command = runner.prepare_command(config, "prompt", home=self.home)
        self.assertEqual(command[command.index("-m") + 1], "test-model")

    def test_prepare_command_resolves_path_without_consulting_cwd(self):
        current = self.root / "current"
        path_directory = self.root / "path"
        current.mkdir()
        path_directory.mkdir()
        if os.name == "nt":
            local_executable = current / "codex.cmd"
            executable = path_directory / "codex.cmd"
        else:
            local_executable = current / "codex"
            executable = path_directory / "codex"
        for path in (local_executable, executable):
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        original_cwd = Path.cwd()
        try:
            os.chdir(current)
            with mock.patch.dict(os.environ, {"PATH": str(path_directory)}):
                command = runner.prepare_command({"cmd": "codex codex"}, "prompt")
            with mock.patch.dict(os.environ, {"PATH": ""}):
                missing = runner.prepare_command({"cmd": "codex"}, "prompt")
        finally:
            os.chdir(original_cwd)
        self.assertEqual(Path(command[0]), executable.resolve())
        self.assertEqual(command[1], "codex")
        self.assertIsNone(missing[0])

    def test_missing_bare_executable_does_not_launch(self):
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with mock.patch.object(runner.subprocess, "run") as run:
                result = runner.run_reviewer(
                    {"name": "missing", "cmd": "missing-reviewer", "stdin": True},
                    "prompt",
                )
        run.assert_not_called()
        self.assertEqual(result.status, "error")
        self.assertEqual(result.seconds, 0.0)
        self.assertEqual(
            result.error, "executable not found on PATH: missing-reviewer"
        )

    def test_prepare_command_substitutes_prompt_last(self):
        prompt = "keep {repo}, {codex_model}, and {output_file} unchanged"
        config = {"cmd": self.command(self.plain) + ' "{prompt}"'}
        command = runner.prepare_command(config, prompt, repo=self.root)
        self.assertEqual(command[-1], prompt)

    def test_codex_output_file_wins_over_echoed_prompt(self):
        fake = self.write_fake(
            "codex_output.py",
            "import pathlib\n"
            "import sys\n"
            "prompt = sys.stdin.read()\n"
            "print('user')\n"
            "print(prompt)\n"
            f"pathlib.Path(sys.argv[1]).write_text({json.dumps(FINDINGS)!r}, encoding='utf-8')\n",
        )
        config = {
            "name": "codex",
            "cmd": self.command(fake) + ' "{output_file}"',
            "stdin": True,
        }
        result = runner.run_reviewer(config, "echoed value: []")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.findings, FINDINGS)
        self.assertIsNone(result.error)

        fallback = self.write_fake("codex_fallback.py", f"print({json.dumps(FINDINGS)!r})\n")
        config["cmd"] = self.command(fallback) + ' "{output_file}"'
        result = runner.run_reviewer(config, "prompt")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.findings, FINDINGS)
        self.assertIn("stdout fallback", result.error)

    def test_codex_log_lines_before_findings_are_accepted(self):
        fake = self.write_fake(
            "codex.py",
            "print('agent log one')\n"
            "print('agent log two')\n"
            "print('agent log three')\n"
            f"print({json.dumps(FINDINGS)!r})\n",
        )
        result = runner.run_reviewer(
            {"name": "codex", "cmd": self.command(fake), "stdin": True},
            "prompt",
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.findings, FINDINGS)

    def test_agy_wrapper_envelope_parses_into_findings_and_usage(self):
        response = f"```json\n{json.dumps(FINDINGS)}\n```"
        result = {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": response,
                "duration_seconds": 1.25,
                "usage": {"input_tokens": 13102, "output_tokens": 22},
            },
        }
        fake = self.write_fake_agy(
            "success", ["not json", json.dumps({"event": "init"}), json.dumps(result)]
        )
        code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 0, stderr)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["model"], "test-model")
        self.assertEqual(envelope["duration_seconds"], 1.25)
        findings, usage = runner.extract_findings(stdout)
        self.assertEqual(findings, FINDINGS)
        self.assertEqual(usage, {"input_tokens": 13102, "output_tokens": 22})

    def test_agy_wrapper_controls_no_tools_preamble(self):
        result = {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "OK"},
        }
        received = self.root / "received.txt"
        fake = self.write_fake_agy(
            "preamble", [json.dumps(result)], capture_path=received
        )
        prompt = "Review this text."
        self.assertEqual(
            agy_llm.NO_TOOLS_PREAMBLE,
            "You have no tools in this task. Do not call any tool, do not read files, "
            "do not run commands, do not browse. Answer from the text below alone, in "
            "exactly the output format it asks for.",
        )
        code, _, stderr = self.run_agy(fake, prompt)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            received.read_text(encoding="utf-8"),
            agy_llm.NO_TOOLS_PREAMBLE + "\n\n" + prompt,
        )
        code, _, stderr = self.run_agy(fake, prompt, "--no-preamble")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(received.read_text(encoding="utf-8"), prompt)

    def test_agy_wrapper_uses_sandbox_in_empty_temporary_cwd(self):
        result = {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "OK"},
        }
        runtime_path = self.root / "runtime.json"
        fake = self.write_fake_agy(
            "sandbox", [json.dumps(result)], runtime_path=runtime_path
        )
        code, _, stderr = self.run_agy(fake)
        self.assertEqual(code, 0, stderr)
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        self.assertIn("--sandbox", runtime["arguments"])
        self.assertEqual(runtime["entries"], [])
        self.assertFalse(Path(runtime["cwd"]).exists())

    def test_agy_wrapper_reports_temporary_directory_creation_failure(self):
        fake = self.write_fake_agy("temporary_directory_failure")
        with mock.patch.object(
            agy_llm.tempfile, "TemporaryDirectory", side_effect=OSError("temp unavailable")
        ):
            code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "agy could not be started: temp unavailable\n")

    def test_agy_wrapper_preserves_reply_when_cleanup_is_locked(self):
        result = {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "OK"},
        }
        fake = self.write_fake_agy("locked_cleanup", [json.dumps(result)])
        cleanup_error = OSError(32, "The process cannot access the directory")

        @contextlib.contextmanager
        def locked_directory(ignore_cleanup_errors=False):
            yield str(self.root)
            if not ignore_cleanup_errors:
                raise cleanup_error

        with mock.patch.object(
            agy_llm.tempfile, "TemporaryDirectory", side_effect=locked_directory
        ) as directory:
            code, stdout, stderr = self.run_agy(fake)
        directory.assert_called_once_with(ignore_cleanup_errors=True)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "OK")
        self.assertEqual(stderr, "")

    def test_agy_wrapper_resolves_relative_executable_before_changing_cwd(self):
        result = {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "OK"},
        }
        fake = self.write_fake_agy("relative", [json.dumps(result)])
        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            relative = f".{os.sep}{fake.name}"
            code, stdout, stderr = self.run_agy(relative)
        finally:
            os.chdir(original_cwd)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "OK")

    def test_find_agy_uses_trusted_path_without_consulting_cwd(self):
        current = self.root / "agy-current"
        path_directory = self.root / "agy-path"
        local_app_data = self.root / "local-app-data"
        current.mkdir()
        path_directory.mkdir()
        local_app_data.mkdir()
        executable_name = "agy.cmd" if os.name == "nt" else "agy"
        local_executable = current / executable_name
        path_executable = path_directory / executable_name
        for path in (local_executable, path_executable):
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
        original_cwd = Path.cwd()
        try:
            os.chdir(current)
            environment = {
                "PATH": "",
                "LOCALAPPDATA": str(local_app_data),
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            }
            with mock.patch.dict(os.environ, environment):
                self.assertIsNone(agy_llm.find_agy())
                self.assertIsNone(agy_llm.find_agy("agy"))
            environment["PATH"] = str(path_directory)
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(
                    Path(agy_llm.find_agy()).resolve(), path_executable.resolve()
                )
                self.assertEqual(
                    Path(agy_llm.find_agy("agy")).resolve(), path_executable.resolve()
                )
        finally:
            os.chdir(original_cwd)

    def test_agy_wrapper_raw_output_is_verbatim(self):
        result = {
            "event": "result",
            "result": {"status": "SUCCESS", "response": '{"n": 9}\n'},
        }
        fake = self.write_fake_agy("raw", [json.dumps(result)])
        code, stdout, stderr = self.run_agy(fake, "prompt", "--raw")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, '{"n": 9}\n')

    def test_agy_wrapper_reports_error_status(self):
        result = {
            "event": "result",
            "result": {"status": "ERROR", "error": "request failed"},
        }
        fake = self.write_fake_agy("error", [json.dumps(result)])
        code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 6)
        self.assertEqual(stdout, "")
        self.assertIn("request failed", stderr)

    def test_agy_wrapper_reports_missing_response(self):
        result = {"event": "result", "result": {"status": "SUCCESS"}}
        fake = self.write_fake_agy("missing_response", [json.dumps(result)])
        code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 6)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "agy returned an empty response\n")

    def test_agy_wrapper_reports_denied_actions(self):
        result = {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "",
                "denied_actions": [{"action": "command"}],
            },
        }
        fake = self.write_fake_agy("denied", [json.dumps(result)])
        code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 6)
        self.assertEqual(stdout, "")
        self.assertIn("denied", stderr)

    def test_agy_wrapper_warns_when_denied_actions_have_a_response(self):
        denied_actions = [{"action": "command", "display_name": "RunCommand"}]
        result = {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "usable review",
                "denied_actions": denied_actions,
            },
        }
        fake = self.write_fake_agy("denied_with_response", [json.dumps(result)])
        code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["denied_actions"], denied_actions)
        self.assertIn("RunCommand", stderr)

    def test_agy_wrapper_rejects_nonzero_exit_after_result(self):
        result = {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "partial review"},
        }
        fake = self.write_fake_agy("nonzero", [json.dumps(result)], exit_code=1)
        code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 7)
        self.assertEqual(stdout, "")
        self.assertIn("agy exited with code 1", stderr)

    def test_agy_wrapper_reports_missing_result(self):
        fake = self.write_fake_agy("garbage", ["not json", "[]"])
        code, stdout, stderr = self.run_agy(fake)
        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertIn("stdout: not json", stderr)

    def test_agy_wrapper_times_out(self):
        fake = self.write_fake_agy("timeout", delay=1)
        code, stdout, stderr = self.run_agy(fake, "prompt", "--timeout", "0.01")
        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        self.assertIn("agy timed out after 0.01 s", stderr)


if __name__ == "__main__":
    unittest.main()
