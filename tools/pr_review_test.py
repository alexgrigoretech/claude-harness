import contextlib
import csv
import datetime
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import agy_llm
import kimi_llm
import muse_llm
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


MUSE_FIXTURE_LINES = [
    '{"schema_version":1,"id":"018f0000-0000-7000-8000-00000000c352","stream":{"kind":"session","id":"01a148d0-d4c4-7ab2-8f99-b7a1bccc68f4"},"sequence":3,"recorded_at":1780531400000002,"record_type":"event","durability":"durable","causation_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","payload_type":"run.model.configured","payload_schema_version":1,"payload":{"kind":"run_model_configured","command_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","run_stream":{"kind":"run","id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d"},"provider_id":"meta","profile_id":"tbh","model_id":"muse-wonder-full-1.0","display_label":"muse-wonder-full-1.0","source":"startup"}}',
    '{"schema_version":1,"id":"018f0000-0000-7000-8000-00000000c354","stream":{"kind":"session","id":"01a148d0-d4c4-7ab2-8f99-b7a1bccc68f4"},"sequence":4,"recorded_at":1780531400000004,"record_type":"status","durability":"ephemeral","causation_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","payload_type":"turn.input.user","payload_schema_version":1,"payload":{"kind":"turn_input_user","command_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","run_stream":{"kind":"run","id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d"},"prompt":"Reply with exactly the word PONG and nothing else."}}',
    '{"schema_version":1,"id":"018f0000-0000-7000-8000-00000000c366","stream":{"kind":"session","id":"01a148d0-d4c4-7ab2-8f99-b7a1bccc68f4"},"sequence":13,"recorded_at":1780531400000022,"record_type":"status","durability":"ephemeral","causation_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","payload_type":"run.output.delta","payload_schema_version":1,"payload":{"kind":"run_output_delta","command_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","run_stream":{"kind":"run","id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d"},"text":"PONG"}}',
    '{"schema_version":1,"id":"018f0000-0000-7000-8000-00000000c36a","stream":{"kind":"session","id":"01a148d0-d4c4-7ab2-8f99-b7a1bccc68f4"},"sequence":15,"recorded_at":1780531400000026,"record_type":"event","durability":"durable","causation_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","payload_type":"task.lifecycle.completed","payload_schema_version":1,"payload":{"kind":"task_lifecycle","command_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","run_stream":{"kind":"run","id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d"},"task_stream":{"kind":"task","id":"01a148d0-dccc-7a30-9851-c743d084bbda"},"task_id":"01a148d0-dccc-7a30-9851-c743d084bbda","event":"completed"}}',
    '{"schema_version":1,"id":"018f0000-0000-7000-8000-00000000c36c","stream":{"kind":"session","id":"01a148d0-d4c4-7ab2-8f99-b7a1bccc68f4"},"sequence":16,"recorded_at":1780531400000028,"record_type":"event","durability":"durable","causation_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","payload_type":"run.terminal.completed","payload_schema_version":1,"payload":{"kind":"run_terminal","command_id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d","run_stream":{"kind":"run","id":"01a148d0-dcaa-7d12-8231-97a783b0fa7d"},"terminal":"completed","text":"PONG","reason":null}}',
]


class PRReviewTest(unittest.TestCase):
    def setUp(self):
        muse_lookup = mock.patch.object(runner, "find_muse", return_value="muse")
        muse_lookup.start()
        self.addCleanup(muse_lookup.stop)
        kimi_lookup = mock.patch.object(runner, "find_kimi", return_value="kimi")
        kimi_lookup.start()
        self.addCleanup(kimi_lookup.stop)
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

    def write_fake_muse(self, mode, lines, exit_code=0, delay=0):
        capture = self.root / f"{mode}.argv.json"
        script = self.write_fake(
            f"{mode}.py",
            textwrap.dedent(
                f"""
                import json
                import os
                import sys
                import time
                from pathlib import Path

                prompt_file = Path(sys.argv[sys.argv.index("--prompt-file") + 1])
                with open({str(capture)!r}, "w", encoding="utf-8") as handle:
                    json.dump({{
                        "arguments": sys.argv[1:],
                        "cwd": os.getcwd(),
                        "entries": os.listdir(),
                        "prompt": prompt_file.read_text(encoding="utf-8"),
                    }}, handle)
                print("muse: workspace root: " + os.getcwd(), file=sys.stderr)
                print("muse: Agent delegation: auto unavailable: workspace is untrusted.", file=sys.stderr)
                time.sleep({delay!r})
                for line in {list(lines)!r}:
                    print(line)
                raise SystemExit({exit_code!r})
                """
            ).lstrip(),
        )
        if os.name == "nt":
            launcher = self.root / f"{mode}.cmd"
            command = subprocess.list2cmdline([sys.executable, str(script)])
            launcher.write_text(f"@echo off\n{command} %*\n", encoding="utf-8")
        else:
            launcher = self.root / f"{mode}.sh"
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            launcher.write_text(f"#!/bin/sh\nexec {command} \"$@\"\n", encoding="utf-8")
            launcher.chmod(0o755)
        return launcher

    def write_fake_kimi(self, mode, lines, exit_code=0, delay=0, sessions=(), stderr=""):
        capture = self.root / f"{mode}.argv.json"
        script = self.write_fake(
            f"{mode}.py",
            textwrap.dedent(
                f"""
                import json
                import os
                import sys
                import time
                from pathlib import Path

                with open({str(capture)!r}, "w", encoding="utf-8") as handle:
                    json.dump({{
                        "arguments": sys.argv[1:],
                        "cwd": os.getcwd(),
                        "entries": os.listdir(),
                        "prompt": Path("review.txt").read_text(encoding="utf-8"),
                        "agent": Path("agent.md").read_text(encoding="utf-8"),
                    }}, handle)
                for entry in {list(sessions)!r}:
                    if entry["workDir"] is None:
                        entry["workDir"] = os.getcwd()
                    session = Path(entry["sessionDir"])
                    session.mkdir(parents=True, exist_ok=True)
                    (session / "wire.jsonl").write_text("private prompt", encoding="utf-8")
                    index = Path(os.environ["CLAUDE_HOOKS_HOME"]) / ".kimi-code" / "session_index.jsonl"
                    with index.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry) + "\\n")
                print({stderr!r}, file=sys.stderr)
                time.sleep({delay!r})
                for line in {list(lines)!r}:
                    print(line)
                raise SystemExit({exit_code!r})
                """
            ).lstrip(),
        )
        if os.name == "nt":
            launcher = self.root / f"{mode}.cmd"
            command = subprocess.list2cmdline([sys.executable, str(script)])
            launcher.write_text(f"@echo off\n{command} %*\n", encoding="utf-8")
        else:
            launcher = self.root / f"{mode}.sh"
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            launcher.write_text(f"#!/bin/sh\nexec {command} \"$@\"\n", encoding="utf-8")
            launcher.chmod(0o755)
        return launcher

    def write_hanging_reviewer(self, mode, pid_file):
        script = self.write_fake(
            f"{mode}.py",
            textwrap.dedent(
                f"""
                import subprocess
                import sys
                import time
                from pathlib import Path

                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                Path({str(pid_file)!r}).write_text(str(child.pid), encoding="utf-8")
                time.sleep(60)
                """
            ).lstrip(),
        )
        if os.name == "nt":
            launcher = self.root / f"{mode}.cmd"
            command = subprocess.list2cmdline([sys.executable, str(script)])
            launcher.write_text(f"@echo off\n{command} %*\n", encoding="utf-8")
        else:
            launcher = self.root / f"{mode}.sh"
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
            launcher.write_text(f"#!/bin/sh\nexec {command} \"$@\"\n", encoding="utf-8")
            launcher.chmod(0o755)
        return launcher

    def write_kimi_config(self, text=None):
        path = self.home / ".kimi-code" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            text = ('[models."kimi-code/kimi-for-coding"]\n'
                    'provider = "managed:kimi-code"\n'
                    'support_efforts = ["low", "high", "max"]\n'
                    'default_effort = "max"\n')
        path.write_bytes(text.encode("utf-8"))
        return path

    def run_kimi(self, fake, prompt="Review this: caf\u00e9", *arguments):
        stdin = io.StringIO(prompt)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}), mock.patch.object(sys, "stdin", stdin):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = kimi_llm.main(["--kimi", str(fake), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def run_muse(self, fake, prompt="Review this: caf\u00e9", *arguments):
        stdin = io.StringIO(prompt)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}), mock.patch.object(sys, "stdin", stdin):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = muse_llm.main(["--muse", str(fake), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

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

    def test_runner_timeout_flag_is_a_positive_integer(self):
        self.assertEqual(runner.build_parser().parse_args([]).timeout, 600)
        self.assertEqual(runner.build_parser().parse_args(["--timeout", "123"]).timeout, 123)
        for value in ("0", "-1", "1.5", "invalid"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    runner.build_parser().parse_args(["--timeout", value])
                self.assertEqual(caught.exception.code, 2)

    def test_runner_passes_timeout_to_reviewers(self):
        for value, expected in ((None, 600), (123, 123)):
            with self.subTest(value=value):
                arguments = [
                    "--repo", str(self.root), "--diff", str(self.diff),
                    "--reviewers", "plain", *self.reviewer_arguments(False),
                ]
                if value is not None:
                    arguments.extend(["--timeout", str(value)])
                result = runner.ReviewerRun("plain", "ok", 0.0, [])
                with mock.patch.object(runner, "run_reviewers", return_value=[result]) as run:
                    with mock.patch.object(runner, "read_body", return_value=""):
                        code, _, stderr = self.run_main(runner.main, arguments)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(run.call_args.kwargs["timeout"], expected)

    def test_prepare_command_passes_reviewer_timeout_with_headroom(self):
        for name in ("kimi", "muse"):
            config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == name)
            for timeout, expected in ((600, "570"), (40, "30"), (30, "30"), (1, "30")):
                with self.subTest(name=name, timeout=timeout):
                    command = runner.prepare_command(config, "prompt", home=self.home, timeout=timeout)
                    self.assertEqual(command[command.index("--timeout") + 1], expected)
            command = runner.prepare_command(config, "prompt", home=self.home)
            self.assertEqual(command[command.index("--timeout") + 1], "270")

    def test_run_reviewer_passes_timeout_to_wrapper_command(self):
        for name in ("kimi", "muse"):
            config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == name)
            for timeout, expected in ((600, "570"), (40, "30")):
                with self.subTest(name=name, timeout=timeout):
                    completed = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
                    with mock.patch.object(runner.subprocess, "run", return_value=completed) as run:
                        result = runner.run_reviewer(config, "prompt", timeout=timeout, home=self.home)
                    self.assertEqual(result.status, "ok", result.error)
                    command = run.call_args.args[0]
                    self.assertEqual(command[command.index("--timeout") + 1], expected)
                    self.assertEqual(run.call_args.kwargs["timeout"], timeout)

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

    def test_scorer_default_selection_prints_optional_reviewer_notice(self):
        cases = self.root / "default-cases"
        case = cases / "sample"
        case.mkdir(parents=True)
        (case / "diff.patch").write_text("diff text", encoding="utf-8")
        (case / "expected.json").write_text("[]", encoding="utf-8")
        result = runner.ReviewerRun("agy", "ok", 0.0, [])
        with mock.patch.object(runner, "run_reviewers", return_value=[result]) as run:
            code, _, stderr = self.run_main(scorer.main, ["--cases", str(cases), "--repo", str(self.root)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual([config["name"] for config in run.call_args.args[1]], ["agy", "codex", "sonnet", "kimi", "muse"])
        self.assertEqual(stderr, "pr-review: haiku skipped (opt-in, pass --reviewers agy,codex,sonnet,kimi,muse,haiku to include it)\n")

    def test_scorer_explicit_selection_keeps_optional_reviewers_silent(self):
        cases = self.root / "explicit-cases"
        case = cases / "sample"
        case.mkdir(parents=True)
        (case / "diff.patch").write_text("diff text", encoding="utf-8")
        (case / "expected.json").write_text("[]", encoding="utf-8")
        result = runner.ReviewerRun("agy", "ok", 0.0, [])
        with mock.patch.object(runner, "run_reviewers", return_value=[result]) as run:
            code, _, stderr = self.run_main(scorer.main, ["--cases", str(cases), "--reviewers", "agy"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual([config["name"] for config in run.call_args.args[1]], ["agy"])
        self.assertEqual(stderr, "")

    def test_scorer_default_all_optional_prints_notice_before_no_reviewers(self):
        configs = {"haiku": {"name": "haiku", "cmd": self.command(self.plain), "optional": True}}
        with mock.patch.object(runner, "load_reviewer_configs", return_value=configs), mock.patch.object(runner, "run_reviewers") as run:
            code, _, stderr = self.run_main(scorer.main, ["--cases", str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "pr-review: haiku skipped (opt-in, pass --reviewers haiku to include it)\nScoring failed: no reviewers are configured or selected.\n")
        run.assert_not_called()

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
        self.assertEqual(scorer.DEFAULT_PRICES["haiku"], (0.10, 0.50))
        self.assertAlmostEqual(scorer.review_cost(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, scorer.DEFAULT_PRICES["haiku"]
        ), 0.60)

    def test_reviewer_override_replaces_default(self):
        override = runner.parse_override(f"agy={self.command(self.plain)}")
        configs = runner.load_reviewer_configs([override], home=self.home)
        self.assertEqual(configs["agy"]["cmd"], self.command(self.plain))
        self.assertTrue(configs["agy"]["stdin"])
        self.assertEqual(list(configs), ["agy", "codex", "sonnet", "haiku", "kimi", "muse"])

    def test_default_reviewer_order(self):
        self.assertEqual([item["name"] for item in runner.DEFAULT_REVIEWERS], ["agy", "codex", "sonnet", "haiku", "kimi", "muse"])

    def test_default_sonnet_reviewer(self):
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "sonnet")
        self.assertEqual(config, {
            "name": "sonnet",
            "cmd": 'claude -p --model claude-sonnet-5-5 --effort medium --no-session-persistence --max-turns 1 --tools "" --append-system-prompt "Reply with the JSON findings array only, no prose."',
            "stdin": True,
        })
        tokens = runner.command_tokens(config["cmd"])
        self.assertEqual(tokens[tokens.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(tokens[tokens.index("--effort") + 1], "medium")
        self.assertIn("--no-session-persistence", tokens)
        self.assertEqual(tokens[tokens.index("--max-turns") + 1], "1")
        self.assertEqual(tokens[tokens.index("--tools") + 1], "")
        self.assertEqual(tokens[tokens.index("--append-system-prompt") + 1], "Reply with the JSON findings array only, no prose.")

    def test_sonnet_price(self):
        self.assertEqual(scorer.DEFAULT_PRICES["sonnet"], (2.00, 10.00))
        self.assertAlmostEqual(scorer.review_cost(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, scorer.DEFAULT_PRICES["sonnet"]
        ), 12.00)

    def test_client_default_selection_notice_includes_sonnet(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(json.dumps({"client": True}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            configs = runner.load_reviewer_configs(home=self.home)
        selected, missing = runner.select_reviewers(configs)
        self.assertEqual([config["name"] for config in selected], ["agy", "codex", "sonnet"])
        self.assertEqual(missing, [])
        self.assertEqual(runner.optional_reviewer_notice(configs, selected), "pr-review: haiku skipped (opt-in, pass --reviewers agy,codex,sonnet,haiku to include it)")

    def test_default_haiku_reviewer(self):
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "haiku")
        self.assertEqual(config, {
            "name": "haiku",
            "cmd": 'claude -p --model claude-haiku-5-5 --no-session-persistence --max-turns 1 --tools "" --append-system-prompt "Reply with the JSON findings array only, no prose."',
            "stdin": True,
            "optional": True,
        })
        tokens = runner.command_tokens(config["cmd"])
        self.assertEqual(tokens[tokens.index("--max-turns") + 1], "1")
        self.assertEqual(tokens[tokens.index("--tools") + 1], "")
        self.assertEqual(tokens[tokens.index("--append-system-prompt") + 1], "Reply with the JSON findings array only, no prose.")

    def test_default_selection_skips_optional_haiku_with_notice(self):
        configs = runner.load_reviewer_configs(home=self.home)
        selected, missing = runner.select_reviewers(configs)
        self.assertEqual([config["name"] for config in selected], ["agy", "codex", "sonnet", "kimi", "muse"])
        self.assertEqual(missing, [])
        result = runner.ReviewerRun("agy", "ok", 0.0, [])
        with mock.patch.object(runner, "run_reviewers", return_value=[result]) as run:
            with mock.patch.object(runner, "read_body", return_value=""):
                code, _, stderr = self.run_main(runner.main, [
                    "--repo", str(self.root), "--diff", str(self.diff),
                ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(run.call_args.args[1], selected)
        self.assertEqual(stderr, "pr-review: haiku skipped (opt-in, pass --reviewers agy,codex,sonnet,kimi,muse,haiku to include it)\n")

    def test_explicit_selection_includes_optional_haiku_without_notice(self):
        configs = runner.load_reviewer_configs(home=self.home)
        selected, missing = runner.select_reviewers(configs, "agy,codex,haiku")
        self.assertEqual([config["name"] for config in selected], ["agy", "codex", "haiku"])
        self.assertEqual(missing, [])
        result = runner.ReviewerRun("haiku", "ok", 0.0, [])
        with mock.patch.object(runner, "run_reviewers", return_value=[result]) as run:
            with mock.patch.object(runner, "read_body", return_value=""):
                code, _, stderr = self.run_main(runner.main, [
                    "--repo", str(self.root), "--diff", str(self.diff),
                    "--reviewers", "agy,codex,haiku",
                ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(run.call_args.args[1], selected)
        self.assertEqual(stderr, "")

    def test_explicit_haiku_alone_runs_only_haiku(self):
        result = runner.ReviewerRun("haiku", "ok", 0.0, [])
        with mock.patch.object(runner, "run_reviewers", return_value=[result]) as run:
            with mock.patch.object(runner, "read_body", return_value=""):
                code, _, stderr = self.run_main(runner.main, [
                    "--repo", str(self.root), "--diff", str(self.diff),
                    "--reviewers", "haiku",
                ])
        self.assertEqual(code, 0, stderr)
        self.assertEqual([config["name"] for config in run.call_args.args[1]], ["haiku"])
        self.assertEqual(stderr, "")

    def test_machine_optional_reviewers_follow_selection_and_notice_rules(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        reviewers = [
            {"name": "plain", "cmd": self.command(self.plain)},
            {"name": "wrapped", "cmd": self.command(self.plain), "optional": True},
            {"name": "other", "cmd": self.command(self.plain), "optional": True},
        ]
        (local / "machine.json").write_text(json.dumps({"pr_review_reviewers": reviewers}), encoding="utf-8")
        configs = runner.load_reviewer_configs(home=self.home)
        self.assertTrue(configs["wrapped"]["optional"])
        self.assertTrue(configs["other"]["optional"])
        for names, expected, notice in (
            (None, ["plain"], "pr-review: wrapped, other skipped (opt-in, pass --reviewers plain,wrapped,other to include them)\n"),
            ("plain,wrapped", ["plain", "wrapped"], ""),
        ):
            with self.subTest(names=names):
                selected, missing = runner.select_reviewers(configs, names)
                self.assertEqual([config["name"] for config in selected], expected)
                self.assertEqual(missing, [])
                arguments = ["--repo", str(self.root), "--diff", str(self.diff)]
                if names is not None:
                    arguments.extend(["--reviewers", names])
                result = runner.ReviewerRun("plain", "ok", 0.0, [])
                with mock.patch.object(runner, "run_reviewers", return_value=[result]) as run:
                    with mock.patch.object(runner, "read_body", return_value=""):
                        code, _, stderr = self.run_main(runner.main, arguments)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(run.call_args.args[1], selected)
                self.assertEqual(stderr, notice)

    def test_reviewer_optional_defaults_to_false(self):
        configs = runner.load_reviewer_configs(home=self.home)
        for name in ("agy", "codex", "kimi", "muse"):
            self.assertIs(configs[name]["optional"], False)
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        reviewers = [{"name": "plain", "cmd": self.command(self.plain)}]
        (local / "machine.json").write_text(json.dumps({"pr_review_reviewers": reviewers}), encoding="utf-8")
        configs = runner.load_reviewer_configs(home=self.home)
        self.assertIs(configs["plain"]["optional"], False)
        self.assertEqual(runner.select_reviewers(configs), ([configs["plain"]], []))

    def test_reviewer_overrides_keep_optional_value(self):
        for optional in (None, False, True):
            with self.subTest(optional=optional):
                override = runner.parse_override(f"haiku={self.command(self.plain)}")
                if optional is not None:
                    override["optional"] = optional
                configs = runner.load_reviewer_configs([override], home=self.home)
                expected = {**override, "optional": True if optional is None else optional}
                self.assertEqual(configs["haiku"], expected)
                selected, missing = runner.select_reviewers(configs)
                self.assertEqual("haiku" in [config["name"] for config in selected], not expected["optional"])
                self.assertEqual(missing, [])

    def test_machine_reviewer_override_preserves_optional_unless_explicit(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        reviewers = [{"name": "plain", "cmd": self.command(self.plain), "optional": True}]
        (local / "machine.json").write_text(json.dumps({"pr_review_reviewers": reviewers}), encoding="utf-8")
        for optional in (None, False, True):
            with self.subTest(optional=optional):
                override = runner.parse_override(f"plain={self.command(self.wrapped)}")
                if optional is not None:
                    override["optional"] = optional
                configs = runner.load_reviewer_configs([override], home=self.home)
                self.assertEqual(configs["plain"]["cmd"], self.command(self.wrapped))
                self.assertIs(configs["plain"]["optional"], True if optional is None else optional)
                selected, missing = runner.select_reviewers(configs)
                self.assertEqual([config["name"] for config in selected], ["plain"] if optional is False else [])
                self.assertEqual(missing, [])

    def test_all_optional_default_selection_prints_opt_in_notice_before_no_reviewers(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        reviewers = [
            {"name": "haiku", "cmd": self.command(self.plain), "optional": True},
            {"name": "other", "cmd": self.command(self.plain), "optional": True},
        ]
        for count in (1, 2):
            with self.subTest(count=count):
                (local / "machine.json").write_text(json.dumps({"pr_review_reviewers": reviewers[:count]}), encoding="utf-8")
                with mock.patch.object(runner, "run_reviewers") as run:
                    code, _, stderr = self.run_main(runner.main, [
                        "--repo", str(self.root), "--diff", str(self.diff),
                    ])
                self.assertEqual(code, 1)
                skipped = [reviewer["name"] for reviewer in reviewers[:count]]
                pronoun = "it" if count == 1 else "them"
                self.assertEqual(stderr, f"pr-review: {', '.join(skipped)} skipped (opt-in, pass --reviewers {','.join(skipped)} to include {pronoun})\nNo reviewers are configured or selected.\n")
                run.assert_not_called()

    def test_optional_notice_excludes_reviewer_made_non_optional_by_override(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        reviewers = [
            {"name": "haiku", "cmd": self.command(self.plain), "optional": True},
            {"name": "other", "cmd": self.command(self.plain), "optional": True},
        ]
        (local / "machine.json").write_text(json.dumps({"pr_review_reviewers": reviewers}), encoding="utf-8")
        override = {**runner.parse_override(f"haiku={self.command(self.wrapped)}"), "optional": False}
        configs = runner.load_reviewer_configs([override], home=self.home)
        selected, missing = runner.select_reviewers(configs)
        self.assertEqual([config["name"] for config in selected], ["haiku"])
        self.assertEqual(missing, [])
        notice = "pr-review: other skipped (opt-in, pass --reviewers haiku,other to include it)"
        self.assertEqual(runner.optional_reviewer_notice(configs, selected), notice)
        result = runner.ReviewerRun("haiku", "ok", 0.0, [])
        with mock.patch.object(runner, "load_reviewer_configs", return_value=configs), mock.patch.object(runner, "run_reviewers", return_value=[result]), mock.patch.object(runner, "read_body", return_value=""):
            code, _, stderr = self.run_main(runner.main, ["--repo", str(self.root), "--diff", str(self.diff)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, notice + "\n")

    def test_optional_notice_returns_none_when_all_reviewers_selected(self):
        configs = runner.load_reviewer_configs(home=self.home)
        self.assertIsNone(runner.optional_reviewer_notice(configs, list(configs.values())))
        self.assertIsNone(runner.optional_reviewer_notice({}, []))
        override = {**runner.parse_override(f"haiku={self.command(self.plain)}"), "optional": False}
        configs = runner.load_reviewer_configs([override], home=self.home)
        self.assertIsNone(runner.optional_reviewer_notice(configs, runner.select_reviewers(configs)[0]))

    def test_explicit_none_keeps_optional_reviewers_silent(self):
        configs = runner.load_reviewer_configs(home=self.home)
        self.assertEqual(runner.select_reviewers(configs, "none"), ([], []))
        with mock.patch.object(runner, "run_reviewers") as run:
            code, _, stderr = self.run_main(runner.main, [
                "--repo", str(self.root), "--diff", str(self.diff), "--reviewers", "none",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "No reviewers are configured or selected.\n")
        run.assert_not_called()

    def test_reviewer_environment_removes_claude_session_markers(self):
        with mock.patch.dict(os.environ, {
            "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "REVIEW_TEST": "kept",
        }):
            for config in runner.DEFAULT_REVIEWERS:
                with self.subTest(reviewer=config["name"]):
                    with mock.patch.object(runner, "prepare_command", return_value=[sys.executable]):
                        with mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess(
                            [], 0, stdout="[]", stderr=""
                        )) as run:
                            result = runner.run_reviewer(config, "prompt")
                    self.assertEqual(result.status, "ok")
                    environment = run.call_args.kwargs["env"]
                    self.assertNotIn("CLAUDECODE", environment)
                    self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", environment)
                    self.assertEqual(environment["REVIEW_TEST"], "kept")
                    self.assertEqual(os.environ["CLAUDECODE"], "1")
                    self.assertEqual(os.environ["CLAUDE_CODE_ENTRYPOINT"], "cli")

    def test_codex_fallback_model(self):
        self.assertEqual(runner.configured_codex_model(self.home), "gpt-6.1-sol")
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        for machine in ({}, {"codex_model": " "}, {"codex_model": 123}, []):
            with self.subTest(machine=machine):
                (local / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
                self.assertEqual(runner.configured_codex_model(self.home), "gpt-6.1-sol")

    def test_claude_and_kimi_resolve_from_path(self):
        directory = self.root / "reviewer-bin"
        directory.mkdir()
        for name in ("claude", "kimi"):
            with self.subTest(name=name):
                executable = directory / (name + ".exe" if os.name == "nt" else name)
                executable.write_text("", encoding="utf-8")
                executable.chmod(0o755)
                with mock.patch.dict(os.environ, {"PATH": str(directory)}):
                    self.assertEqual(Path(runner.resolve_reviewer_executable(name)), executable.resolve())

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
        prompt = "keep {repo}, {codex_model}, {output_file}, and {reviewer_timeout} unchanged"
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


    def test_default_kimi_reviewer(self):
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "kimi")
        self.assertTrue(config["stdin"])
        self.assertEqual(runner.command_tokens(config["cmd"]), [
            sys.executable, str(Path(runner.__file__).with_name("kimi_llm.py")),
            "--model", "{kimi_model}", "--effort", "{kimi_effort}", "--timeout", "{reviewer_timeout}",
        ])
        self.assertEqual([item["name"] for item in runner.DEFAULT_REVIEWERS], ["agy", "codex", "sonnet", "haiku", "kimi", "muse"])

    def test_kimi_wrapper_reuses_muse_helpers(self):
        from tools import muse_llm as shared_muse

        for name in ("_find_on_path", "is_client_machine", "run_prompt"):
            with self.subTest(name=name):
                self.assertIs(getattr(kimi_llm, name), getattr(shared_muse, name))

    def test_kimi_wrapper_runs_as_script_from_tools_directory(self):
        fake = self.write_fake_kimi("kimi_tools_directory", ['{"role":"assistant","content":"PONG"}'])
        script = Path(kimi_llm.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), "--kimi", str(fake), "--raw"],
            cwd=script.parent,
            env={**os.environ, "CLAUDE_HOOKS_HOME": str(self.home), "PYTHONPATH": ""},
            input="Reply with PONG.",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "PONG")
        self.assertEqual(result.stderr, "")

    def test_prepare_command_uses_configured_kimi_model(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        path = local / "machine.json"
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "kimi")
        self.assertEqual(runner.configured_kimi_model(self.home), "kimi-code/kimi-for-coding")
        for value in ("kimi-code/another-model", "Model_1.0-test", "0"):
            with self.subTest(value=value):
                path.write_text(json.dumps({"kimi_model": value}), encoding="utf-8")
                command = runner.prepare_command(config, "prompt", home=self.home)
                self.assertEqual(command[command.index("--model") + 1], value)
        for value in (None, [], 123, "", " ", "kimi&echo", "-kimi", "kimi\n", " kimi"):
            with self.subTest(value=value):
                path.write_text(json.dumps({"kimi_model": value}), encoding="utf-8")
                self.assertEqual(runner.configured_kimi_model(self.home), "kimi-code/kimi-for-coding")
        with mock.patch.object(runner, "configured_kimi_model") as model:
            runner.prepare_command({"cmd": self.command(self.plain)}, "{kimi_model}", home=self.home)
        model.assert_not_called()

    def test_prepare_command_uses_configured_kimi_effort(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        path = local / "machine.json"
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "kimi")
        self.assertEqual(runner.configured_kimi_effort(self.home), "")
        command = runner.prepare_command(config, "prompt", home=self.home)
        self.assertEqual(command[command.index("--effort") + 1], "")
        for value in ("low", "high", "max"):
            with self.subTest(value=value):
                path.write_text(json.dumps({"kimi_model": "kimilow/kimi-for-coding", "kimi_effort": value}), encoding="utf-8")
                self.assertEqual(runner.configured_kimi_effort(self.home), value)
                command = runner.prepare_command(config, "prompt", home=self.home)
                self.assertEqual(command[command.index("--model") + 1], "kimilow/kimi-for-coding")
                self.assertEqual(command[command.index("--effort") + 1], value)
        for value in (None, "", " ", "medium", "LOW", 1, [], {}):
            with self.subTest(value=value):
                path.write_text(json.dumps({"kimi_effort": value}), encoding="utf-8")
                self.assertEqual(runner.configured_kimi_effort(self.home), "")
                command = runner.prepare_command(config, "prompt", home=self.home)
                self.assertEqual(command[command.index("--effort") + 1], "")
        with mock.patch.object(runner, "configured_kimi_effort") as effort:
            runner.prepare_command({"cmd": self.command(self.plain)}, "{kimi_effort}", home=self.home)
        effort.assert_not_called()

    def test_client_machine_skips_default_kimi_reviewer(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(json.dumps({"client": True}), encoding="utf-8")
        stderr = io.StringIO()
        with mock.patch.object(runner, "find_kimi") as find, contextlib.redirect_stderr(stderr):
            configs = runner.load_reviewer_configs(home=self.home)
        self.assertEqual(list(configs), ["agy", "codex", "sonnet", "haiku"])
        self.assertIn("pr-review: kimi skipped (client machine, no diff leaves the machine)", stderr.getvalue())
        find.assert_not_called()

    def test_explicit_kimi_reviewer_bypasses_default_gate(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        override = runner.parse_override(f"kimi={self.command(self.plain)}")
        for machine, overrides in (
            ({"client": True}, [override]),
            ({"client": True, "pr_review_reviewers": [override]}, []),
        ):
            with self.subTest(machine=machine):
                (local / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
                with mock.patch.object(runner, "find_kimi", return_value=None) as find:
                    with contextlib.redirect_stderr(io.StringIO()):
                        configs = runner.load_reviewer_configs(overrides, home=self.home)
                self.assertEqual(configs["kimi"], {**override, "optional": False})
                find.assert_not_called()

    def test_missing_kimi_executable_skips_default_reviewer(self):
        environment = {"PATH": "", "APPDATA": str(self.root / "empty-app-data")}
        stderr = io.StringIO()
        with mock.patch.object(runner, "find_kimi", side_effect=kimi_llm.find_kimi):
            with mock.patch.dict(os.environ, environment), contextlib.redirect_stderr(stderr):
                configs = runner.load_reviewer_configs(home=self.home)
        self.assertEqual(list(configs), ["agy", "codex", "sonnet", "haiku", "muse"])
        self.assertEqual(stderr.getvalue(), "pr-review: kimi skipped (Kimi Code not installed)\n")

    def test_kimi_prices_are_zero(self):
        self.assertEqual(scorer.DEFAULT_PRICES["kimi"], (0.0, 0.0))
        self.assertEqual(scorer.review_cost(None, scorer.DEFAULT_PRICES["kimi"]), 0.0)

    def test_kimi_wrapper_writes_prompt_file_and_read_only_agent(self):
        prompt = "Review this: caf\u00e9\n" + "private prompt " * 5000
        fake = self.write_fake_kimi("kimi_files", ['{"role":"assistant","content":"PONG"}'])
        code, stdout, stderr = self.run_kimi(fake, prompt)
        self.assertEqual(code, 0, stderr)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["response"], "PONG")
        self.assertEqual(envelope["usage"], {})
        self.assertEqual(envelope["model"], "kimi-code/kimi-for-coding")
        self.assertGreaterEqual(envelope["duration_seconds"], 0)
        runtime = json.loads((self.root / "kimi_files.argv.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["arguments"], [
            "--agent-file", "agent.md", "-m", "kimi-code/kimi-for-coding", "-p",
            "Read review.txt in the current directory and follow its instructions. Reply with the answer only.",
            "--output-format", "stream-json",
        ])
        self.assertEqual(set(runtime["entries"]), {"review.txt", "agent.md"})
        self.assertEqual(runtime["prompt"], prompt)
        self.assertEqual(runtime["agent"], "---\nname: review-reader\ndescription: Read-only reviewer\ntools:\n  - Read\n---\nYou have one tool, Read. Read the file named in the prompt, then answer with exactly the output format that file asks for and nothing else.\n")
        self.assertFalse({"--yolo", "--auto", "--plan"}.intersection(runtime["arguments"]))
        self.assertNotIn(prompt, runtime["arguments"])
        self.assertNotEqual(Path(runtime["cwd"]), Path.cwd())
        self.assertFalse(Path(runtime["cwd"]).exists())
        self.assertEqual(stderr, "")

    def test_kimi_wrapper_envelope_parses_into_findings(self):
        lines = [json.dumps({"role": "assistant", "content": json.dumps(FINDINGS)})]
        fake = self.write_fake_kimi("kimi_findings", lines)
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--model", "test/model")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["model"], "test/model")
        findings, usage = runner.extract_findings(stdout)
        self.assertEqual(findings, FINDINGS)
        self.assertIsNone(usage)
        config = {
            "name": "kimi", "stdin": True,
            "cmd": self.command(Path(kimi_llm.__file__)) + " --kimi " + subprocess.list2cmdline([str(fake)]),
        }
        with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            result = runner.run_reviewer(config, "prompt", home=self.home)
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(result.findings, FINDINGS)

    def test_kimi_wrapper_reports_missing_assistant_line(self):
        lines = [
            '{"role":"meta","type":"system.version","version":"2.2.0"}',
            '{"role":"user","content":"PRIVATE_PROMPT_TEST_MARKER"}',
            '{"role":"assistant","content":"","tool_calls":[{"name":"Read"}]}',
            'error: PRIVATE_PROMPT_TEST_MARKER',
        ]
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                fake = self.write_fake_kimi("kimi_missing", lines, exit_code=exit_code, stderr="failure " + "x" * 500)
                code, stdout, stderr = self.run_kimi(fake)
                self.assertEqual(code, 5)
                self.assertEqual(stdout, "")
                self.assertIn("kimi returned no assistant line", stderr)
                self.assertIn('roles: ["assistant", "meta", "user"]', stderr)
                self.assertIn('types: ["system.version"]', stderr)
                self.assertIn("stderr: failure " + "x" * 392, stderr)
                self.assertNotIn("x" * 393, stderr)
                self.assertNotIn("PRIVATE_PROMPT_TEST_MARKER", stderr)

    def test_kimi_wrapper_reports_nonzero_exit_after_answer(self):
        fake = self.write_fake_kimi("kimi_nonzero", ['{"role":"assistant","content":"PONG"}'], exit_code=9)
        code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 7)
        self.assertEqual(stdout, "")
        self.assertIn("kimi exited with code 9", stderr)

    def test_kimi_wrapper_times_out(self):
        fake = self.write_fake_kimi("kimi_timeout", [], delay=0.2)
        with mock.patch.object(kimi_llm, "cleanup_sessions") as cleanup:
            code, stdout, stderr = self.run_kimi(fake, "prompt", "--timeout", "0.01")
        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        self.assertIn("kimi timed out after 0.01 s", stderr)
        cleanup.assert_called_once()

    def test_wrapper_timeouts_kill_sleeping_children(self):
        if os.name == "nt":
            probe = subprocess.run(
                ["tasklist", "/FI", f"PID eq {os.getpid()}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="replace", timeout=5, check=False,
            )
            if probe.returncode != 0:
                self.skipTest(f"tasklist is unavailable: {probe.stderr.strip()}")

        def child_is_running(pid):
            if os.name == "nt":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, errors="replace", timeout=5, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return any(len(row) > 1 and row[1] == str(pid) for row in csv.reader(result.stdout.splitlines()))
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        for name, run_wrapper in (("kimi", self.run_kimi), ("muse", self.run_muse)):
            with self.subTest(wrapper=name):
                pid_file = self.root / f"{name}-child.pid"
                fake = self.write_hanging_reviewer(f"{name}_child_timeout", pid_file)
                try:
                    started = time.monotonic()
                    code, stdout, stderr = run_wrapper(fake, "prompt", "--timeout", "1")
                    duration = time.monotonic() - started
                    self.assertEqual(code, 4, stderr)
                    self.assertEqual(stdout, "")
                    self.assertIn(f"{name} timed out after 1 s", stderr)
                    self.assertLess(duration, 6)
                    self.assertTrue(pid_file.is_file(), "the fake must spawn a child before timing out")
                    child_pid = int(pid_file.read_text(encoding="utf-8"))
                    deadline = time.monotonic() + 3
                    while child_is_running(child_pid) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertFalse(child_is_running(child_pid), f"child {child_pid} survived the timeout")
                    if os.name != "nt":
                        with self.assertRaises(ProcessLookupError):
                            os.kill(child_pid, 0)
                finally:
                    if pid_file.is_file():
                        child_pid = int(pid_file.read_text(encoding="utf-8"))
                        if child_is_running(child_pid):
                            if os.name == "nt":
                                subprocess.run(
                                    ["taskkill", "/T", "/F", "/PID", str(child_pid)],
                                    capture_output=True, timeout=10, check=False,
                                )
                            else:
                                try:
                                    os.kill(child_pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass

    def test_wrapper_timeout_survives_cleanup_and_drain_failures(self):
        for name, run_wrapper in (("kimi", self.run_kimi), ("muse", self.run_muse)):
            with self.subTest(wrapper=name):
                process = mock.Mock(pid=12345)
                process.communicate.side_effect = subprocess.TimeoutExpired("fake", 1)
                fake = self.write_fake_muse(f"{name}_drain_timeout", [])
                with mock.patch.object(muse_llm.subprocess, "Popen", return_value=process):
                    with mock.patch.object(muse_llm.subprocess, "run", side_effect=OSError("kill failed")) as kill:
                        with mock.patch.object(os, "getpgid", return_value=12345, create=True):
                            with mock.patch.object(os, "killpg", side_effect=OSError("kill failed"), create=True):
                                code, stdout, stderr = run_wrapper(fake, "prompt", "--timeout", "1")
                self.assertEqual((code, stdout), (4, ""))
                self.assertEqual(stderr, f"{name} timed out after 1 s\n")
                self.assertEqual(process.communicate.call_args_list, [mock.call(timeout=1), mock.call(timeout=10)])
                if os.name == "nt":
                    kill.assert_called_once_with(
                        ["taskkill", "/T", "/F", "/PID", "12345"],
                        capture_output=True, check=False, timeout=10,
                    )

    def test_run_prompt_kills_posix_process_group(self):
        process = mock.Mock(pid=12345, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 1), ("", "")]
        with mock.patch.object(muse_llm.subprocess, "Popen", return_value=process) as launch:
            with mock.patch.object(os, "getpgid", return_value=12345, create=True) as group:
                with mock.patch.object(os, "killpg", create=True) as kill:
                    with mock.patch.object(signal, "SIGKILL", 9, create=True):
                        with mock.patch.object(os, "name", "posix"):
                            with self.assertRaises(subprocess.TimeoutExpired):
                                muse_llm.run_prompt(["fake"], 1, "fake-directory")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        group.assert_called_once_with(12345)
        kill.assert_called_once_with(12345, 9)
        self.assertEqual(process.communicate.call_args_list, [mock.call(timeout=1), mock.call(timeout=10)])

    def test_kimi_wrapper_preserves_answer_before_trailing_empty_messages(self):
        lines = [
            '{"role":"assistant","content":"PONG"}',
            '{"role":"assistant","content":"","tool_calls":[{}]}',
            '{"role":"assistant","content":""}',
        ]
        fake = self.write_fake_kimi("kimi_trailing_empty", lines)
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--raw")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "PONG")
        self.assertEqual(kimi_llm.parse_events("\n".join(lines))[0], "PONG")

    def test_kimi_wrapper_raw_output_and_no_preamble(self):
        fake = self.write_fake_kimi("kimi_raw", ['{"role":"assistant","content":"PONG"}'])
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--raw", "--no-preamble")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "PONG")
        self.assertIn("no-preamble is ignored", stderr)

    def test_kimi_wrapper_reports_empty_response(self):
        for content in ("", " \n"):
            with self.subTest(content=content):
                fake = self.write_fake_kimi("kimi_empty", [json.dumps({"role": "assistant", "content": content})])
                code, stdout, stderr = self.run_kimi(fake)
                self.assertEqual(code, 6)
                self.assertEqual(stdout, "")
                self.assertIn("kimi returned an empty response", stderr)

    def test_kimi_events_use_last_answer_and_resume_hint(self):
        lines = ["not json", "[]", "null", '{"role":"assistant","content":17}',
                 '{"role":"assistant","content":"first"}',
                 '{"role":"assistant","content":"last"}',
                 '{"role":"assistant","content":"","tool_calls":[{}]}',
                 '{"role":"assistant","content":"","tool_calls":[]}',
                 '{"role":"meta","type":"session.resume_hint","session_id":"test-session"}']
        response, session_id, roles, types = kimi_llm.parse_events("\n".join(lines))
        self.assertEqual(response, "last")
        self.assertEqual(session_id, "test-session")
        self.assertEqual(roles, {"assistant", "meta"})
        self.assertEqual(types, {"session.resume_hint"})

    def test_kimi_wrapper_client_gate(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        path = local / "machine.json"
        fake = self.write_fake_kimi("kimi_gate", ['{"role":"assistant","content":"PONG"}'])
        for content, reason in ((json.dumps({"client": True}), "client machine"),
                                ("not json", "machine.json unreadable"),
                                ("[]", "machine.json unreadable")):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                with mock.patch.object(kimi_llm, "find_kimi") as find:
                    code, stdout, stderr = self.run_kimi(fake)
                self.assertEqual(code, 8)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, f"kimi refused: {reason}, nothing leaves this machine\n")
                find.assert_not_called()
                code, stdout, stderr = self.run_kimi(fake, "prompt", "--allow-client", "--raw")
                self.assertEqual(code, 0, stderr)
                self.assertEqual(stdout, "PONG")
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 8)
        self.assertIn("machine.json unreadable", stderr)

    def test_kimi_wrapper_missing_and_non_client_machine_proceed(self):
        fake = self.write_fake_kimi("kimi_personal", ['{"role":"assistant","content":"PONG"}'])
        code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 0, stderr)
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        for machine in ({}, {"client": False}, {"client": "true"}):
            with self.subTest(machine=machine):
                (local / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
                code, stdout, stderr = self.run_kimi(fake)
                self.assertEqual(code, 0, stderr)

    def test_kimi_wrapper_reports_empty_stdin_and_missing_executable(self):
        missing = self.root / "missing-kimi"
        code, stdout, stderr = self.run_kimi(missing, "")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "prompt on stdin is empty\n")
        code, stdout, stderr = self.run_kimi(missing)
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "kimi was not found. Install it with: npm install -g @moonshot-ai/kimi-code\n")

    def test_kimi_wrapper_reports_start_failure(self):
        fake = self.write_fake_kimi("kimi_start", [])
        with mock.patch.object(kimi_llm.subprocess, "Popen", side_effect=OSError("cannot start")):
            code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertIn("kimi could not be started: cannot start", stderr)

    def test_kimi_wrapper_defaults_and_model_validation(self):
        args = kimi_llm.parser().parse_args([])
        self.assertEqual(args.timeout, 540)
        self.assertEqual(args.model, "kimi-code/kimi-for-coding")
        self.assertEqual(args.effort, "")
        for value in ("", "low", "high", "max"):
            self.assertEqual(kimi_llm.parser().parse_args(["--effort", value]).effort, value)
        for value in ("medium", "LOW", " "):
            with self.subTest(effort=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    kimi_llm.parser().parse_args(["--effort", value])
                self.assertEqual(caught.exception.code, 2)
        for value in ("kimi-code/kimi-for-coding", "Model_1.0-test", "0"):
            self.assertEqual(kimi_llm.parser().parse_args(["--model", value]).model, value)
        for value in ("", "kimi&echo", "kimi|echo", "kimi model", "-kimi", "kimi\n", " kimi"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    kimi_llm.parser().parse_args(["--model", value])
                self.assertEqual(caught.exception.code, 2)

    def test_kimi_alias_already_present_preserves_bytes(self):
        path = self.write_kimi_config('[models."kimilow/kimi-for-coding"]\r\ndefault_effort = "high"\r\n')
        original = path.read_bytes()
        fake = self.write_fake_kimi("kimi_existing_alias", ['{"role":"assistant","content":"PONG"}'])
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--model", "kimilow/kimi-for-coding", "--effort", "low")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertEqual(stderr, "")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_created_atomically_with_daily_backup(self):
        path = self.write_kimi_config()
        original = path.read_bytes() + b'\n[providers."managed:kimi-code"]\ntype = "kimi"\n'
        path.write_bytes(original)
        fake = self.write_fake_kimi("kimi_new_alias", ['{"role":"assistant","content":"PONG"}'])
        with mock.patch.object(kimi_llm.os, "replace", wraps=os.replace) as replace:
            code, stdout, stderr = self.run_kimi(fake, "prompt", "--model", "kimilow/kimi-for-coding", "--effort", "low")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["model"], "kimilow/kimi-for-coding")
        self.assertEqual(stderr, f"kimi alias: re-created kimilow/kimi-for-coding from kimi-code/kimi-for-coding with default_effort low in {path}\n")
        replace.assert_called_once()
        self.assertEqual(Path(replace.call_args.args[0]).parent, path.parent)
        self.assertEqual(replace.call_args.args[1], path)
        self.assertTrue(path.read_bytes().startswith(original))
        models = kimi_llm.tomllib.loads(path.read_text(encoding="utf-8"))["models"]
        self.assertEqual(models["kimilow/kimi-for-coding"], dict(models["kimi-code/kimi-for-coding"], default_effort="low"))
        backup = path.with_name(f"config.toml.bak-kimi-llm-{datetime.date.today().isoformat()}")
        self.assertEqual(backup.read_bytes(), original)
        self.assertIn("re-created", kimi_llm.ensure_alias("kimihigh/kimi-for-coding", "high", path))
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(len(list(path.parent.glob("config.toml.bak-kimi-llm-*"))), 1)
        self.assertEqual(set(path.parent.iterdir()), {path, backup})

    def test_kimi_alias_appends_missing_default_effort(self):
        path = self.write_kimi_config('[models."kimi-code/kimi-for-coding"]\nprovider = "managed:kimi-code"')
        original = path.read_bytes()
        message = kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path)
        self.assertIn("re-created", message)
        self.assertTrue(path.read_bytes().startswith(original + b"\n\n"))
        self.assertEqual(kimi_llm.tomllib.loads(path.read_text(encoding="utf-8"))["models"]["kimilow/kimi-for-coding"]["default_effort"], "low")

    def test_kimi_alias_preserves_crlf(self):
        path = self.write_kimi_config('[models."kimi-code/kimi-for-coding"]\r\nprovider = "managed:kimi-code"\r\ndefault_effort = "max"\r\n\r\n')
        original = path.read_bytes()
        self.assertIn("re-created", kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path))
        result = path.read_bytes()
        self.assertTrue(result.startswith(original))
        self.assertNotIn(b"\n", result.replace(b"\r\n", b""))
        self.assertTrue(result.endswith(b'default_effort = "low"\r\n'))

    def test_kimi_alias_replaces_quoted_default_effort(self):
        path = self.write_kimi_config('[models."kimi-code/kimi-for-coding"]\nprovider = "managed:kimi-code"\n"default_effort" = "max"\n')
        original = path.read_bytes()
        self.assertIn("re-created", kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path))
        self.assertTrue(path.read_bytes().startswith(original))
        models = kimi_llm.tomllib.loads(path.read_text(encoding="utf-8"))["models"]
        self.assertEqual(models["kimi-code/kimi-for-coding"]["default_effort"], "max")
        self.assertEqual(models["kimilow/kimi-for-coding"]["default_effort"], "low")

    def test_kimi_alias_refuses_invalid_rebuilt_block(self):
        path = self.write_kimi_config('[models."kimi-code/kimi-for-coding"]\nprovider = "managed:kimi-code"\ndefault_effort = "max"\nmatrix = [\n[1, 2],\n[3, 4],\n]\n')
        original = path.read_bytes()
        kimi_llm.tomllib.loads(original.decode("utf-8"))
        fake = self.write_fake_kimi("kimi_invalid_block", ['{"role":"assistant","content":"PONG"}'])
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--model", "kimilow/kimi-for-coding", "--effort", "low")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertTrue(stderr.startswith(f"kimi alias: refusing to write {path}, the rebuilt block is not valid TOML: "))
        self.assertEqual(stderr.count("kimi alias:"), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_non_table_models_warns_and_review_continues(self):
        fake = self.write_fake_kimi("kimi_non_table", ['{"role":"assistant","content":"PONG"}'])
        for text in ('models = "not a table"\n', 'models = "kimilow/kimi-for-coding"\n', 'models = 1\n',
                     '[models]\n"kimi-code/kimi-for-coding" = "not a table"\n',
                     '[models]\n"kimi-code/kimi-for-coding" = []\n'):
            with self.subTest(text=text):
                path = self.write_kimi_config(text)
                original = path.read_bytes()
                code, stdout, stderr = self.run_kimi(fake, "prompt", "--model", "kimilow/kimi-for-coding", "--effort", "low")
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["response"], "PONG")
                self.assertTrue(stderr.startswith("kimi alias: "))
                self.assertEqual(stderr.count("kimi alias:"), 1)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_invalid_utf8_warns_and_review_continues(self):
        path = self.write_kimi_config()
        original = b'\xff[models."kimi-code/kimi-for-coding"]\n'
        path.write_bytes(original)
        fake = self.write_fake_kimi("kimi_invalid_utf8", ['{"role":"assistant","content":"PONG"}'])
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--model", "kimilow/kimi-for-coding", "--effort", "low")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertTrue(stderr.startswith("kimi alias: "))
        self.assertIn("'utf-8' codec can't decode byte", stderr)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_backup_uses_private_exclusive_creation(self):
        path = self.write_kimi_config()
        original = path.read_bytes()
        backup = path.with_name(f"config.toml.bak-kimi-llm-{datetime.date.today().isoformat()}")
        with mock.patch.object(kimi_llm.os, "open", wraps=os.open) as opened:
            self.assertIn("re-created", kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path))
        opened.assert_any_call(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.assertEqual(backup.read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "POSIX permissions and symlinks require a non Windows platform")
    def test_kimi_alias_preserves_private_mode_and_symlink_target(self):
        path = self.write_kimi_config()
        path.chmod(0o600)
        original = path.read_bytes()
        link = self.home / "config-link.toml"
        link.symlink_to(path)
        previous_umask = os.umask(0)
        try:
            message = kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", link)
        finally:
            os.umask(previous_umask)
        self.assertIn(f"default_effort low in {path}", message)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(path))
        self.assertEqual(link.resolve(), path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(link.read_bytes(), path.read_bytes())
        self.assertTrue(path.read_bytes().startswith(original))
        backup = path.with_name(f"config.toml.bak-kimi-llm-{datetime.date.today().isoformat()}")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.read_bytes(), original)

    def test_kimi_alias_accepts_single_quoted_base_header(self):
        path = self.write_kimi_config("[models.'kimi-code/kimi-for-coding']\nprovider = \"managed:kimi-code\"\ndefault_effort = \"max\"\n")
        original = path.read_bytes()
        self.assertIn("re-created", kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path))
        self.assertTrue(path.read_bytes().startswith(original))
        models = kimi_llm.tomllib.loads(path.read_text(encoding="utf-8"))["models"]
        self.assertEqual(models["kimilow/kimi-for-coding"], dict(models["kimi-code/kimi-for-coding"], default_effort="low"))

    def test_kimi_alias_header_and_exact_final_newline(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                text = newline.join(('[models."kimi-code/kimi-for-coding"]', 'provider = "managed:kimi-code"', 'default_effort = "max"', "", "", ""))
                path = self.write_kimi_config(text)
                original = path.read_bytes()
                self.assertIn("re-created", kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path))
                appended = newline.join(('[models."kimilow/kimi-for-coding"]', 'provider = "managed:kimi-code"', 'default_effort = "low"', ""))
                self.assertEqual(path.read_bytes(), original + appended.encode("utf-8"))
                self.assertFalse(path.read_bytes().endswith((newline * 2).encode("utf-8")))

    def test_kimi_alias_under_provider_prefix_warns(self):
        path = self.write_kimi_config()
        original = path.read_bytes()
        model = "kimi-code/low-alias"
        self.assertEqual(kimi_llm.ensure_alias(model, "low", path), f"kimi alias: {model} sits under the kimi-code/ prefix, the Kimi CLI model refresh deletes such aliases, name it outside the prefix in kimi_model")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertIsNone(kimi_llm.ensure_alias("kimi-code/kimi-for-coding", "low", path))

    def test_kimi_alias_without_slash_warns(self):
        path = self.write_kimi_config()
        original = path.read_bytes()
        self.assertEqual(kimi_llm.ensure_alias("kimilow", "low", path), "kimi alias: kimilow has no kimi-code/ base to copy, running the model as configured")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_missing_base_warns(self):
        path = self.write_kimi_config("[models]\n")
        original = path.read_bytes()
        self.assertEqual(kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path), f"kimi alias: base kimi-code/kimi-for-coding is missing from {path}, run kimi login, running the model as configured")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_unsupported_effort_warns(self):
        path = self.write_kimi_config('[models."kimi-code/kimi-for-coding"]\nsupport_efforts = ["high", "max"]\n')
        original = path.read_bytes()
        self.assertEqual(kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path), "kimi alias: kimi-code/kimi-for-coding does not support effort low (supports: high, max)")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_missing_config_warns_and_review_continues(self):
        path = self.home / ".kimi-code" / "config.toml"
        fake = self.write_fake_kimi("kimi_missing_config", ['{"role":"assistant","content":"PONG"}'])
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--effort", "low")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertEqual(stderr, f"kimi alias: {path} not found, running the model as configured\n")
        self.assertEqual(list(self.home.iterdir()), [])

    def test_kimi_alias_empty_effort_never_reads_config(self):
        path = self.home / ".kimi-code" / "config.toml"
        original_open = Path.open
        def guarded_open(candidate, *arguments, **keywords):
            self.assertNotEqual(candidate, path)
            return original_open(candidate, *arguments, **keywords)
        fake = self.write_fake_kimi("kimi_no_effort", ['{"role":"assistant","content":"PONG"}'])
        for arguments in ((), ("--effort", "")):
            with self.subTest(arguments=arguments):
                with mock.patch.object(Path, "open", guarded_open), mock.patch.object(kimi_llm, "ensure_alias") as ensure:
                    code, stdout, stderr = self.run_kimi(fake, "prompt", *arguments)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["response"], "PONG")
                self.assertEqual(stderr, "")
                self.assertEqual(list(self.home.iterdir()), [])
                ensure.assert_not_called()

    def test_kimi_alias_effort_with_wrapper_options_and_client_gate(self):
        path = self.write_kimi_config()
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(json.dumps({"client": True}), encoding="utf-8")
        fake = self.write_fake_kimi("kimi_alias_options", ['{"role":"assistant","content":"PONG"}'])
        with mock.patch.object(kimi_llm, "ensure_alias") as ensure:
            code, stdout, stderr = self.run_kimi(fake, "prompt", "--effort", "low")
        self.assertEqual(code, 8)
        self.assertEqual(stdout, "")
        self.assertIn("client machine", stderr)
        ensure.assert_not_called()
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--allow-client", "--model", "kimilow/kimi-for-coding", "--effort", "low", "--raw", "--no-preamble")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "PONG")
        self.assertIn(f"default_effort low in {path}", stderr)
        self.assertIn("no-preamble is ignored", stderr)

    def test_kimi_alias_lock_timeout_preserves_config(self):
        path = self.write_kimi_config()
        original = path.read_bytes()
        lock = path.with_name("config.toml.kimi-llm.lock")
        lock.write_text("held", encoding="utf-8")
        with mock.patch.object(kimi_llm.time, "monotonic", side_effect=[0, 5]):
            message = kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path)
        self.assertIn("kimi alias: config lock unavailable", message)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(lock.read_text(encoding="utf-8"), "held")
        self.assertEqual(set(path.parent.iterdir()), {path, lock})

    def test_kimi_alias_rechecks_config_after_lock(self):
        path = self.write_kimi_config()
        original = path.read_bytes()
        concurrent = original + b'\n[models."kimilow/kimi-for-coding"]\ndefault_effort = "high"\n'
        acquire = kimi_llm._acquire_session_lock
        def concurrent_alias(lock_path):
            path.write_bytes(concurrent)
            return acquire(lock_path)
        with mock.patch.object(kimi_llm, "_acquire_session_lock", side_effect=concurrent_alias):
            self.assertIsNone(kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path))
        self.assertEqual(path.read_bytes(), concurrent)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_invalid_toml_warns_without_writing(self):
        path = self.write_kimi_config("[models\n")
        original = path.read_bytes()
        message = kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path)
        self.assertTrue(message.startswith("kimi alias: "))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_read_failure_warns_without_writing(self):
        path = self.write_kimi_config()
        original = path.read_bytes()
        with mock.patch.object(Path, "open", side_effect=PermissionError("cannot read config")):
            self.assertEqual(kimi_llm.ensure_alias("kimilow/kimi-for-coding", "low", path), "kimi alias: cannot read config")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_kimi_alias_replace_failure_preserves_config_and_review(self):
        path = self.write_kimi_config()
        original = path.read_bytes()
        fake = self.write_fake_kimi("kimi_alias_failure", ['{"role":"assistant","content":"PONG"}'])
        with mock.patch.object(kimi_llm.os, "replace", side_effect=OSError("cannot replace config")):
            code, stdout, stderr = self.run_kimi(fake, "prompt", "--model", "kimilow/kimi-for-coding", "--effort", "low")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertEqual(stderr, "kimi alias: cannot replace config\n")
        self.assertEqual(path.read_bytes(), original)
        backup = path.with_name(f"config.toml.bak-kimi-llm-{datetime.date.today().isoformat()}")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(set(path.parent.iterdir()), {path, backup})

    def test_kimi_wrapper_removes_own_session(self):
        sessions = self.home / ".kimi-code" / "sessions"
        own = sessions / "wd_own" / "own-session"
        hinted = sessions / "wd_hinted" / "hinted-session"
        other = sessions / "wd_other" / "other-session"
        retained = {"sessionId": "other-session", "sessionDir": str(other), "workDir": str(self.root / "other")}
        records = [
            {"sessionId": "own-session", "sessionDir": str(own), "workDir": None},
            {"sessionId": "hinted-session", "sessionDir": str(hinted), "workDir": str(self.root / "alias")},
            retained,
        ]
        lines = ['{"role":"assistant","content":"PONG"}',
                 '{"role":"meta","type":"session.resume_hint","session_id":"hinted-session"}']
        fake = self.write_fake_kimi("kimi_sessions", lines, sessions=records)
        code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertFalse(own.parent.exists())
        self.assertFalse(hinted.parent.exists())
        self.assertTrue((other / "wire.jsonl").exists())
        index = self.home / ".kimi-code" / "session_index.jsonl"
        self.assertEqual(index.read_text(encoding="utf-8"), json.dumps(retained) + "\n")
        self.assertEqual(sorted(item.name for item in index.parent.iterdir()), ["session_index.jsonl", "sessions"])

    def test_kimi_wrapper_skips_index_rewrite_when_lock_is_held(self):
        base = self.home / ".kimi-code"
        base.mkdir()
        lock = base / "session_index.jsonl.lock"
        lock.write_text("held", encoding="utf-8")
        session = base / "sessions" / "wd_locked" / "locked-session"
        records = [{"sessionId": "locked-session", "sessionDir": str(session), "workDir": None}]
        fake = self.write_fake_kimi("kimi_held_lock", ['{"role":"assistant","content":"PONG"}'], sessions=records)
        with mock.patch.object(kimi_llm.os, "replace") as replace:
            code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertFalse(session.parent.exists())
        self.assertEqual(lock.read_text(encoding="utf-8"), "held")
        entries = (base / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(entries), 1)
        self.assertEqual(json.loads(entries[0])["sessionId"], "locked-session")
        self.assertEqual(stderr.count("kimi session cleanup warning:"), 1)
        self.assertIn("index rewrite skipped", stderr)
        replace.assert_not_called()

    def test_kimi_wrapper_takes_stale_session_index_lock(self):
        base = self.home / ".kimi-code"
        base.mkdir()
        lock = base / "session_index.jsonl.lock"
        lock.write_text("stale", encoding="utf-8")
        stale = time.time() - 90
        os.utime(lock, (stale, stale))
        session = base / "sessions" / "wd_stale" / "stale-session"
        records = [{"sessionId": "stale-session", "sessionDir": str(session), "workDir": None}]
        fake = self.write_fake_kimi("kimi_stale_lock", ['{"role":"assistant","content":"PONG"}'], sessions=records)
        code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertEqual(stderr, "")
        self.assertFalse(session.parent.exists())
        self.assertFalse(lock.exists())
        self.assertEqual((base / "session_index.jsonl").read_bytes(), b"")

    def test_kimi_session_index_lock_retries_at_point_two_seconds(self):
        lock = self.root / "session_index.jsonl.lock"
        lock.write_text("held", encoding="utf-8")
        with mock.patch.object(kimi_llm.time, "monotonic", side_effect=[0, 0, 5]):
            with mock.patch.object(kimi_llm.time, "sleep") as sleep:
                with self.assertRaises(TimeoutError):
                    kimi_llm._acquire_session_lock(lock)
        sleep.assert_called_once_with(0.2)
        self.assertEqual(lock.read_text(encoding="utf-8"), "held")

    def test_kimi_wrapper_preserves_malformed_index_lines(self):
        base = self.home / ".kimi-code"
        session = base / "sessions" / "wd_garbage" / "test-session"
        session.mkdir(parents=True)
        outside = self.root / "outside-session"
        outside.mkdir()
        index = base / "session_index.jsonl"
        prefix = 'garbage before\r\n[]\nnull\r\n{"sessionId":"test-session"}\n'
        suffix = '\r\nfalse\n{"sessionId":"test-session","sessionDir":null}\r\n'
        suffix += json.dumps({"sessionId": "test-session", "sessionDir": "bad\x00path"}) + "\n"
        suffix += json.dumps({"sessionId": "test-session", "sessionDir": str(outside)}) + "\n"
        suffix += "garbage after\r\n"
        entry = {"sessionId": "test-session", "sessionDir": str(session), "workDir": str(self.root)}
        index.write_bytes((prefix + json.dumps(entry) + "\n" + suffix).encode("utf-8"))
        lines = ['{"role":"assistant","content":"PONG"}',
                 '{"role":"meta","type":"session.resume_hint","session_id":"test-session"}']
        fake = self.write_fake_kimi("kimi_garbage_index", lines)
        code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertEqual(stderr, "")
        self.assertFalse(session.parent.exists())
        self.assertTrue(outside.is_dir())
        self.assertEqual(index.read_bytes(), (prefix + suffix).encode("utf-8"))
        self.assertFalse(index.with_name(index.name + ".lock").exists())

    def test_kimi_wrapper_cleans_temp_and_lock_after_replace_failure(self):
        base = self.home / ".kimi-code"
        session = base / "sessions" / "wd_replace" / "test-session"
        session.mkdir(parents=True)
        index = base / "session_index.jsonl"
        lock = base / "session_index.jsonl.lock"
        entry = {"sessionId": "test-session", "sessionDir": str(session), "workDir": str(self.root)}
        original = (json.dumps(entry) + "\n").encode("utf-8")
        index.write_bytes(original)
        lines = ['{"role":"assistant","content":"PONG"}',
                 '{"role":"meta","type":"session.resume_hint","session_id":"test-session"}']
        fake = self.write_fake_kimi("kimi_replace_failure", lines)

        def fail_replace(source, destination):
            self.assertTrue(lock.is_file())
            self.assertEqual(Path(source).parent, base)
            self.assertEqual(destination, index)
            raise PermissionError("index is held open")

        with mock.patch.object(kimi_llm.os, "replace", side_effect=fail_replace) as replace:
            code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        self.assertFalse(session.parent.exists())
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(stderr.count("kimi session cleanup warning:"), 1)
        self.assertIn("index is held open", stderr)
        self.assertFalse(Path(replace.call_args.args[0]).exists())
        self.assertEqual(sorted(path.name for path in base.iterdir()), ["session_index.jsonl", "sessions"])

    def test_kimi_events_accept_text_parts_and_ignore_empty_parts(self):
        parts = [
            {"type": "text", "text": "PO"},
            {"type": "image", "text": "ignored"},
            {"type": "text", "text": 17},
            {"text": "ignored"},
            "ignored",
            None,
            {"type": "text", "text": "NG"},
        ]
        lines = [json.dumps({"role": "assistant", "content": parts}),
                 json.dumps({"role": "assistant", "content": [{"type": "image"}]}),
                 json.dumps({"role": "assistant", "content": []}),
                 '{"role":"assistant","content":""}']
        self.assertEqual(kimi_llm.parse_events("\n".join(lines))[0], "PONG")
        fake = self.write_fake_kimi("kimi_text_parts", lines)
        code, stdout, stderr = self.run_kimi(fake, "prompt", "--raw")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "PONG")

    def test_kimi_wrapper_tolerates_unreadable_session_index(self):
        index = self.home / ".kimi-code" / "session_index.jsonl"
        index.mkdir(parents=True)
        for lines, expected in ((['{"role":"assistant","content":"PONG"}'], 0), ([], 5)):
            with self.subTest(expected=expected):
                fake = self.write_fake_kimi("kimi_bad_index", lines)
                code, stdout, stderr = self.run_kimi(fake)
                self.assertEqual(code, expected)
                self.assertEqual(stderr.count("kimi session cleanup warning:"), 1)
        index.rmdir()
        index.write_text("not json\n", encoding="utf-8")
        code, stdout, stderr = self.run_kimi(fake)
        self.assertEqual(code, 5)
        self.assertEqual(stderr.count("kimi session cleanup warning:"), 0)

    @unittest.skipUnless(os.name == "nt", "Windows session path comparison")
    def test_kimi_session_cleanup_compares_resolved_windows_paths(self):
        session = self.home / ".kimi-code" / "sessions" / "wd_case" / "session"
        session.mkdir(parents=True)
        index = self.home / ".kimi-code" / "session_index.jsonl"
        entry = {"sessionId": "case-session", "sessionDir": str(session), "workDir": str(self.root).upper()}
        index.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            kimi_llm.cleanup_sessions(self.root / "child" / "..")
        self.assertFalse(session.parent.exists())
        self.assertEqual(index.read_text(encoding="utf-8"), "")

    def test_unreadable_machine_drops_both_default_subscription_reviewers(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        path = local / "machine.json"
        for content in ("not json", "[]", "null"):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    configs = runner.load_reviewer_configs(home=self.home)
                self.assertEqual(list(configs), ["agy", "codex", "sonnet", "haiku"])
                self.assertEqual(stderr.getvalue().count("machine.json unreadable"), 2)
        stderr = io.StringIO()
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            with contextlib.redirect_stderr(stderr):
                configs = runner.load_reviewer_configs(home=self.home)
        self.assertNotIn("kimi", configs)
        self.assertNotIn("muse", configs)
        self.assertEqual(stderr.getvalue().count("machine.json unreadable"), 2)

    def test_find_kimi_skips_current_directory_and_honours_path(self):
        current = self.root / "current"
        directory = self.root / "path"
        current.mkdir()
        directory.mkdir()
        name = "kimi.cmd" if os.name == "nt" else "kimi"
        for parent in (current, directory):
            executable = parent / name
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
        previous = Path.cwd()
        try:
            os.chdir(current)
            environment = {
                "PATH": os.pathsep.join([".", str(current), str(directory)]),
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "APPDATA": str(self.root / "no-app-data"),
            }
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(Path(kimi_llm.find_kimi()), directory / name)
                self.assertEqual(Path(kimi_llm.find_kimi("kimi")), directory / name)
                self.assertEqual(Path(kimi_llm.find_kimi("./" + name)), current / name)
                self.assertIsNone(kimi_llm.find_kimi(str(current / "missing")))
        finally:
            os.chdir(previous)

    @unittest.skipUnless(os.name == "nt", "Windows install fallback")
    def test_find_kimi_uses_windows_install_fallback(self):
        app_data = self.root / "app-data"
        executable = app_data / "npm" / "kimi.cmd"
        executable.parent.mkdir(parents=True)
        executable.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PATH": "", "APPDATA": str(app_data)}):
            self.assertEqual(Path(kimi_llm.find_kimi()), executable)
            self.assertIsNone(kimi_llm.find_kimi("missing-kimi"))

    def test_default_muse_reviewer(self):
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "muse")
        self.assertTrue(config["stdin"])
        self.assertEqual(runner.command_tokens(config["cmd"]), [
            sys.executable, str(Path(runner.__file__).with_name("muse_llm.py")),
            "--model", "{muse_model}", "--effort", "{muse_effort}",
            "--timeout", "{reviewer_timeout}",
        ])
        self.assertEqual(runner.DEFAULT_REVIEWERS[-1], config)

    def test_prepare_command_uses_configured_muse_model_and_effort(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(
            json.dumps({"muse_model": "test-model", "muse_effort": " max "}), encoding="utf-8"
        )
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "muse")
        command = runner.prepare_command(config, "prompt", home=self.home)
        self.assertEqual(command[command.index("--model") + 1], "test-model")
        self.assertEqual(command[command.index("--effort") + 1], "max")

    def test_muse_configuration_falls_back(self):
        self.assertEqual(runner.configured_muse_model(self.home), "muse-wonder-full-1.0")
        self.assertEqual(runner.configured_muse_effort(self.home), "medium")
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        for machine in ({}, [], None, {"muse_model": None, "muse_effort": None},
                        {"muse_model": " ", "muse_effort": " "},
                        {"muse_model": 123, "muse_effort": []}):
            with self.subTest(machine=machine):
                (local / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
                self.assertEqual(runner.configured_muse_model(self.home), "muse-wonder-full-1.0")
                self.assertEqual(runner.configured_muse_effort(self.home), "medium")
        (local / "machine.json").write_text("not json", encoding="utf-8")
        self.assertEqual(runner.configured_muse_model(self.home), "muse-wonder-full-1.0")
        self.assertEqual(runner.configured_muse_effort(self.home), "medium")

    def test_prepare_command_only_reads_muse_config_when_needed(self):
        config = {"cmd": self.command(self.plain)}
        with mock.patch.object(runner, "configured_muse_model") as model:
            with mock.patch.object(runner, "configured_muse_effort") as effort:
                runner.prepare_command(config, "{muse_model} {muse_effort}", home=self.home)
        model.assert_not_called()
        effort.assert_not_called()

    def test_muse_effort_out_of_range_falls_back(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "muse")
        for value in ("ultra", " MAX ", "invalid"):
            with self.subTest(value=value):
                (local / "machine.json").write_text(json.dumps({"muse_effort": value}), encoding="utf-8")
                self.assertEqual(runner.configured_muse_effort(self.home), "medium")
                command = runner.prepare_command(config, "prompt", home=self.home)
                self.assertEqual(command[command.index("--effort") + 1], "medium")
        for value in ("minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(value=value):
                (local / "machine.json").write_text(json.dumps({"muse_effort": f" {value} "}), encoding="utf-8")
                self.assertEqual(runner.configured_muse_effort(self.home), value)

    def test_muse_model_invalid_ids_fall_back(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "muse")
        for value in ("muse&echo", "muse|echo", "muse model", "-muse", "muse\n", " muse", "muse/model"):
            with self.subTest(model=value):
                (local / "machine.json").write_text(json.dumps({"muse_model": value}), encoding="utf-8")
                self.assertEqual(runner.configured_muse_model(self.home), "muse-wonder-full-1.0")
                command = runner.prepare_command(config, "prompt", home=self.home)
                self.assertEqual(command[command.index("--model") + 1], "muse-wonder-full-1.0")
        for value in ("muse-wonder-full-1.0", "Model_1.0-test", "0"):
            with self.subTest(model=value):
                (local / "machine.json").write_text(json.dumps({"muse_model": value}), encoding="utf-8")
                self.assertEqual(runner.configured_muse_model(self.home), value)

    def test_client_machine_skips_default_muse_reviewer(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(json.dumps({"client": True}), encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            configs = runner.load_reviewer_configs(home=self.home)
        self.assertEqual(list(configs), ["agy", "codex", "sonnet", "haiku"])
        self.assertEqual(stderr.getvalue(), "pr-review: muse skipped (client machine, no diff leaves the machine)\npr-review: kimi skipped (client machine, no diff leaves the machine)\n")

    def test_client_machine_keeps_explicit_muse_reviewer(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        path = local / "machine.json"
        override = runner.parse_override(f"muse={self.command(self.plain)}")
        for machine, overrides in (
            ({"client": True}, [override]),
            ({"client": True, "pr_review_reviewers": [override]}, []),
        ):
            with self.subTest(machine=machine):
                path.write_text(json.dumps(machine), encoding="utf-8")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    configs = runner.load_reviewer_configs(overrides, home=self.home)
                self.assertEqual(configs["muse"], {**override, "optional": False})
                self.assertEqual(stderr.getvalue(), "pr-review: kimi skipped (client machine, no diff leaves the machine)\n" if overrides else "")

    def test_unreadable_machine_skips_default_muse_reviewer(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        path = local / "machine.json"
        for content in ("not json", '{"client": false,', "\ufffd"):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    configs = runner.load_reviewer_configs(home=self.home)
                self.assertEqual(list(configs), ["agy", "codex", "sonnet", "haiku"])
                self.assertEqual(stderr.getvalue(), "pr-review: muse skipped (machine.json unreadable)\npr-review: kimi skipped (machine.json unreadable)\n")
        override = runner.parse_override(f"muse={self.command(self.plain)}")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            configs = runner.load_reviewer_configs([override], home=self.home)
        self.assertEqual(configs["muse"], {**override, "optional": False})
        self.assertEqual(stderr.getvalue(), "pr-review: kimi skipped (machine.json unreadable)\n")

    def test_missing_machine_keeps_default_muse_reviewer(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            configs = runner.load_reviewer_configs(home=self.home)
        self.assertIn("muse", configs)
        self.assertEqual(stderr.getvalue(), "")

    def test_default_muse_reviewer_requires_installed_cli(self):
        environment = {
            "PATH": "",
            "LOCALAPPDATA": str(self.root / "empty-local-app-data"),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        }
        with mock.patch.object(runner, "find_muse", side_effect=muse_llm.find_muse):
            with mock.patch.dict(os.environ, environment):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    configs = runner.load_reviewer_configs(home=self.home)
                self.assertNotIn("muse", configs)
                self.assertEqual(stderr.getvalue(), "pr-review: muse skipped (Muse Code not installed)\n")
            executable = self.root / ("muse.cmd" if os.name == "nt" else "muse")
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
            environment["PATH"] = str(self.root)
            with mock.patch.dict(os.environ, environment):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    configs = runner.load_reviewer_configs(home=self.home)
                self.assertIn("muse", configs)
                self.assertEqual(stderr.getvalue(), "")

    def test_muse_skip_message_requires_removed_entry(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        defaults = [item for item in runner.DEFAULT_REVIEWERS if item["name"] not in {"muse", "kimi"}]
        for client in (True, False):
            with self.subTest(client=client):
                (local / "machine.json").write_text(json.dumps({"client": client}), encoding="utf-8")
                stderr = io.StringIO()
                with mock.patch.object(runner, "DEFAULT_REVIEWERS", defaults):
                    with mock.patch.object(runner, "find_muse", return_value=None) as find:
                        with contextlib.redirect_stderr(stderr):
                            configs = runner.load_reviewer_configs(home=self.home)
                self.assertNotIn("muse", configs)
                self.assertEqual(stderr.getvalue(), "")
                find.assert_not_called()

    def test_missing_muse_install_keeps_explicit_reviewer(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        override = runner.parse_override(f"muse={self.command(self.plain)}")
        for machine, overrides in (({}, [override]), ({"pr_review_reviewers": [override]}, [])):
            with self.subTest(machine=machine):
                (local / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
                stderr = io.StringIO()
                with mock.patch.object(runner, "find_muse", return_value=None) as find:
                    with contextlib.redirect_stderr(stderr):
                        configs = runner.load_reviewer_configs(overrides, home=self.home)
                self.assertEqual(configs["muse"], {**override, "optional": False})
                self.assertEqual(stderr.getvalue(), "")
                find.assert_not_called()

    def test_muse_prices_are_zero(self):
        self.assertEqual(scorer.DEFAULT_PRICES["muse"], (0.0, 0.0))
        self.assertEqual(scorer.review_cost(None, scorer.DEFAULT_PRICES["muse"]), 0.0)

    def test_muse_wrapper_argv_carries_data_policy_flags(self):
        fake = self.write_fake_muse("muse_argv", MUSE_FIXTURE_LINES)
        prompt = "Review caf\u00e9 & echo not-a-command"
        code, stdout, stderr = self.run_muse(fake, prompt)
        self.assertEqual(code, 0, stderr)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["response"], "PONG")
        self.assertEqual(envelope["usage"], {})
        self.assertEqual(envelope["model"], "muse-wonder-full-1.0")
        self.assertIsInstance(envelope["duration_seconds"], float)
        self.assertGreaterEqual(envelope["duration_seconds"], 0)
        runtime = json.loads((self.root / "muse_argv.argv.json").read_text(encoding="utf-8"))
        prompt_file = str(Path(runtime["cwd"]) / "prompt.txt")
        self.assertEqual(runtime["arguments"], [
            "exec", "--json", "--model", "muse-wonder-full-1.0",
            "--reasoning-effort", "medium", "--approval-mode", "never",
            "--contributor-mode", "off", "--no-foreign-personal-context",
            "--no-session-log", "--disable-web-tools", "--disable-reminders",
            "--disable-request-user-input", "--disable-shell", "--disable-write",
            "--max-model-steps", "4", "--prompt-file", prompt_file,
        ])
        self.assertNotIn(prompt, runtime["arguments"])
        self.assertNotEqual(Path(runtime["cwd"]), Path.cwd())
        self.assertEqual(runtime["entries"], ["prompt.txt"])
        self.assertEqual(runtime["prompt"], agy_llm.NO_TOOLS_PREAMBLE + "\n\n" + prompt)
        self.assertEqual(stderr, "")

    def test_muse_wrapper_envelope_parses_into_findings(self):
        terminal = json.loads(MUSE_FIXTURE_LINES[-1])
        terminal["payload"]["text"] = json.dumps(FINDINGS)
        fake = self.write_fake_muse("muse_findings", MUSE_FIXTURE_LINES[:-1] + [json.dumps(terminal)])
        code, stdout, stderr = self.run_muse(fake, "prompt", "--model", "requested-model")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["model"], "muse-wonder-full-1.0")
        findings, usage = runner.extract_findings(stdout)
        self.assertEqual(findings, FINDINGS)
        self.assertIsNone(usage)
        config = {
            "name": "muse", "stdin": True,
            "cmd": self.command(Path(muse_llm.__file__)) + " --muse " + subprocess.list2cmdline([str(fake)]),
        }
        with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.home)}):
            result = runner.run_reviewer(config, "prompt", home=self.home)
        self.assertEqual(result.status, "ok", result.error)
        self.assertEqual(result.findings, FINDINGS)

    def test_muse_wrapper_reports_incomplete_terminal(self):
        terminal = json.loads(MUSE_FIXTURE_LINES[-1])
        terminal["payload"].update(terminal="interrupted", reason="step limit")
        fake = self.write_fake_muse("muse_incomplete", MUSE_FIXTURE_LINES[:-1] + [json.dumps(terminal)])
        code, stdout, stderr = self.run_muse(fake)
        self.assertEqual(code, 6)
        self.assertEqual(stdout, "")
        self.assertIn("muse run ended with terminal interrupted: step limit", stderr)

    def test_muse_wrapper_reports_missing_terminal(self):
        prompt = "PRIVATE_PROMPT_TEST_MARKER"
        user_event = json.dumps({"payload_type": "turn.input.user", "payload": {"prompt": prompt}})
        fake = self.write_fake_muse("muse_missing", [user_event, "not json", "[]"] + MUSE_FIXTURE_LINES[:-1])
        code, stdout, stderr = self.run_muse(fake)
        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertIn("no run.terminal record", stderr)
        self.assertIn('payload types: ["run.model.configured", "run.output.delta", "task.lifecycle.completed", "turn.input.user"]', stderr)
        self.assertNotIn("stdout:", stderr)
        self.assertNotIn("not json", stderr)
        self.assertNotIn(prompt, stderr)
        self.assertNotIn("Reply with exactly the word PONG", stderr)
        self.assertIn("stderr: muse: workspace root:", stderr)

    def test_muse_wrapper_reports_failed_terminal(self):
        for value in ("failed", "cancelled", "future"):
            with self.subTest(terminal=value):
                terminal = json.loads(MUSE_FIXTURE_LINES[-1])
                terminal["payload_type"] = f"run.terminal.{value}"
                terminal["payload"].update(terminal=value, reason="budget")
                fake = self.write_fake_muse(f"muse_{value}", MUSE_FIXTURE_LINES + [json.dumps(terminal)])
                code, stdout, stderr = self.run_muse(fake)
                self.assertEqual((code, stdout), (6, ""))
                self.assertIn(f"muse run ended with terminal {value}: budget", stderr)

    def test_muse_wrapper_times_out(self):
        fake = self.write_fake_muse("muse_timeout", MUSE_FIXTURE_LINES, delay=1)
        code, stdout, stderr = self.run_muse(fake, "prompt", "--timeout", "0.01")
        self.assertEqual(code, 4)
        self.assertEqual(stdout, "")
        self.assertIn("muse timed out after 0.01 s", stderr)

    def test_muse_wrapper_raw_output_and_no_preamble(self):
        fake = self.write_fake_muse("muse_raw", MUSE_FIXTURE_LINES)
        code, stdout, stderr = self.run_muse(
            fake, "prompt", "--raw", "--no-preamble", "--effort", "max", "--max-steps", "2"
        )
        self.assertEqual((code, stdout, stderr), (0, "PONG", ""))
        runtime = json.loads((self.root / "muse_raw.argv.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["prompt"], "prompt")
        arguments = runtime["arguments"]
        self.assertEqual(arguments[arguments.index("--reasoning-effort") + 1], "max")
        self.assertEqual(arguments[arguments.index("--max-model-steps") + 1], "2")

    def test_muse_wrapper_uses_delta_fallback_and_requested_model(self):
        terminal = json.loads(MUSE_FIXTURE_LINES[-1])
        terminal["payload"].pop("text")
        delta = json.loads(MUSE_FIXTURE_LINES[2])
        delta["payload"]["text"] = "!"
        fake = self.write_fake_muse("muse_fallback", [
            MUSE_FIXTURE_LINES[2], json.dumps(delta), json.dumps(terminal),
        ])
        code, stdout, stderr = self.run_muse(fake, "prompt", "--model", "requested-model")
        self.assertEqual(code, 0, stderr)
        envelope = json.loads(stdout)
        self.assertEqual(envelope["response"], "PONG!")
        self.assertEqual(envelope["model"], "requested-model")

    def test_muse_wrapper_uses_last_terminal_and_ignores_unknown_records(self):
        terminal = json.loads(MUSE_FIXTURE_LINES[-1])
        terminal["payload"].update(text="last response")
        fake = self.write_fake_muse("muse_last", MUSE_FIXTURE_LINES + [
            "not json", "[]", "null", '{"payload_type":"future","payload":{}}',
            '{"payload_type":"run.output.delta","payload":[]}', json.dumps(terminal),
        ])
        code, stdout, stderr = self.run_muse(fake)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "last response")

    def test_muse_wrapper_reports_empty_response(self):
        terminal = json.loads(MUSE_FIXTURE_LINES[-1])
        terminal["payload"]["text"] = " "
        fake = self.write_fake_muse("muse_empty", [json.dumps(terminal)])
        code, stdout, stderr = self.run_muse(fake)
        self.assertEqual(code, 6)
        self.assertEqual(stdout, "")
        self.assertIn("muse returned an empty response", stderr)

    def test_muse_wrapper_rejects_nonzero_exit_after_terminal(self):
        terminal = json.loads(MUSE_FIXTURE_LINES[-1])
        terminal["payload"]["text"] = ""
        for index, lines in enumerate((MUSE_FIXTURE_LINES, [json.dumps(terminal)])):
            with self.subTest(lines=lines):
                fake = self.write_fake_muse(f"muse_nonzero_{index}", lines, exit_code=1)
                code, stdout, stderr = self.run_muse(fake)
                self.assertEqual((code, stdout), (7, ""))
                self.assertIn("muse exited with code 1", stderr)
                self.assertNotIn("muse returned an empty response", stderr)

    def test_muse_wrapper_terminal_status_precedes_process_exit(self):
        terminal = json.loads(MUSE_FIXTURE_LINES[-1])
        terminal["payload_type"] = "run.terminal.failed"
        terminal["payload"].update(terminal="failed", text="", reason="budget")
        for lines, expected in ((MUSE_FIXTURE_LINES[:-1], 5), ([json.dumps(terminal)], 6)):
            with self.subTest(expected=expected):
                fake = self.write_fake_muse(f"muse_exit_order_{expected}", lines, exit_code=1)
                code, stdout, stderr = self.run_muse(fake)
                self.assertEqual((code, stdout), (expected, ""))
                self.assertNotIn("muse exited with code 1", stderr)

    def test_muse_wrapper_defaults_timeout_and_shared_efforts(self):
        arguments = muse_llm.parser().parse_args([])
        self.assertEqual(arguments.timeout, 240)
        self.assertEqual(runner.MUSE_EFFORTS, muse_llm.MUSE_EFFORTS)
        choices = next(action.choices for action in muse_llm.parser()._actions if action.dest == "effort")
        self.assertIs(choices, muse_llm.MUSE_EFFORTS)
        fake = self.write_fake_muse("muse_default_timeout", MUSE_FIXTURE_LINES)
        process = mock.Mock(returncode=0)
        process.communicate.return_value = ("\n".join(MUSE_FIXTURE_LINES), "")
        with mock.patch.object(muse_llm.subprocess, "Popen", return_value=process) as launch:
            code, _, stderr = self.run_muse(fake)
        self.assertEqual(code, 0, stderr)
        process.communicate.assert_called_once_with(timeout=240)
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(launch.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.PIPE)
        if os.name == "nt":
            self.assertEqual(launch.call_args.kwargs["creationflags"], subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(launch.call_args.kwargs["start_new_session"])

    def test_muse_wrapper_rejects_invalid_model_ids(self):
        fake = self.write_fake_muse("muse_invalid_model", MUSE_FIXTURE_LINES)
        for value in ("", "muse&echo", "muse|echo", "muse model", "-muse", "muse\n", " muse", "muse/model"):
            with self.subTest(model=value):
                with mock.patch.object(muse_llm, "find_muse") as find:
                    with self.assertRaises(SystemExit) as raised:
                        self.run_muse(fake, "prompt", f"--model={value}")
                self.assertEqual(raised.exception.code, 2)
                find.assert_not_called()
                self.assertFalse((self.root / "muse_invalid_model.argv.json").exists())
        for value in ("muse-wonder-full-1.0", "Model_1.0-test", "0"):
            with self.subTest(model=value):
                self.assertEqual(muse_llm.parser().parse_args(["--model", value]).model, value)

    def test_muse_wrapper_reports_empty_stdin_and_missing_executable(self):
        missing = self.root / "missing-muse.cmd"
        code, stdout, stderr = self.run_muse(missing, "")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("prompt on stdin is empty", stderr)
        code, stdout, stderr = self.run_muse(missing)
        self.assertEqual((code, stdout), (3, ""))
        self.assertEqual(stderr, "muse was not found. Install it with:\nWindows: irm https://dev.meta.ai/install.ps1 | iex\nmacOS or Linux: curl -fsSL https://dev.meta.ai/install.sh | bash\n")

    def test_muse_wrapper_refuses_client_machine(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(json.dumps({"client": True}), encoding="utf-8")
        fake = self.write_fake_muse("muse_client_refused", MUSE_FIXTURE_LINES)
        paths = sorted(self.root.rglob("*"))
        with mock.patch.object(muse_llm, "find_muse") as find:
            with mock.patch.object(muse_llm.tempfile, "TemporaryDirectory") as directory:
                code, stdout, stderr = self.run_muse(fake)
        self.assertEqual((code, stdout), (8, ""))
        self.assertEqual(stderr, "muse refused: client machine, nothing leaves this machine\n")
        self.assertFalse((self.root / "muse_client_refused.argv.json").exists())
        self.assertEqual(sorted(self.root.rglob("*")), paths)
        find.assert_not_called()
        directory.assert_not_called()

    def test_muse_wrapper_allow_client_bypasses_gate(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(json.dumps({"client": True}), encoding="utf-8")
        fake = self.write_fake_muse("muse_client_allowed", MUSE_FIXTURE_LINES)
        code, stdout, stderr = self.run_muse(fake, "prompt", "--allow-client")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["response"], "PONG")
        arguments = json.loads((self.root / "muse_client_allowed.argv.json").read_text(encoding="utf-8"))["arguments"]
        self.assertEqual(arguments[arguments.index("--contributor-mode") + 1], "off")
        self.assertIn("--no-foreign-personal-context", arguments)

    def test_muse_wrapper_missing_or_non_client_machine_does_not_block(self):
        fake = self.write_fake_muse("muse_client_unknown", MUSE_FIXTURE_LINES)
        code, _, stderr = self.run_muse(fake)
        self.assertEqual(code, 0, stderr)
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        for content in ("{}", '{"client": false}'):
            with self.subTest(content=content):
                (local / "machine.json").write_text(content, encoding="utf-8")
                code, _, stderr = self.run_muse(fake)
                self.assertEqual(code, 0, stderr)

    def test_muse_wrapper_refuses_unreadable_machine(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        fake = self.write_fake_muse("muse_unreadable", MUSE_FIXTURE_LINES)
        for content in ("not json", "[]", "null", "123", '"client"'):
            with self.subTest(content=content):
                (local / "machine.json").write_text(content, encoding="utf-8")
                paths = sorted(self.root.rglob("*"))
                with mock.patch.object(muse_llm, "find_muse") as find:
                    with mock.patch.object(muse_llm.tempfile, "TemporaryDirectory") as directory:
                        code, stdout, stderr = self.run_muse(fake)
                self.assertEqual((code, stdout), (8, ""))
                self.assertEqual(stderr, "muse refused: machine.json unreadable, nothing leaves this machine\n")
                self.assertFalse((self.root / "muse_unreadable.argv.json").exists())
                self.assertEqual(sorted(self.root.rglob("*")), paths)
                find.assert_not_called()
                directory.assert_not_called()
        code, _, stderr = self.run_muse(fake, "prompt", "--allow-client")
        self.assertEqual(code, 0, stderr)

    def test_muse_wrapper_refuses_machine_read_error(self):
        fake = self.write_fake_muse("muse_read_error", MUSE_FIXTURE_LINES)
        with mock.patch.object(muse_llm.Path, "open", side_effect=PermissionError("denied")):
            code, stdout, stderr = self.run_muse(fake)
        self.assertEqual((code, stdout), (8, ""))
        self.assertEqual(stderr, "muse refused: machine.json unreadable, nothing leaves this machine\n")
        self.assertFalse((self.root / "muse_read_error.argv.json").exists())

    def test_muse_client_gate_uses_hooks_home_rule(self):
        local = self.home / ".claude" / "local"
        local.mkdir(parents=True)
        (local / "machine.json").write_text(json.dumps({"client": True}), encoding="utf-8")
        with mock.patch.object(muse_llm.Path, "home", return_value=self.home):
            with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": ""}):
                self.assertTrue(muse_llm.is_client_machine())
            with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_HOME": str(self.root / "other-home")}):
                self.assertFalse(muse_llm.is_client_machine())

    def test_muse_wrapper_reports_start_failure(self):
        fake = self.write_fake_muse("muse_start_failure", MUSE_FIXTURE_LINES)
        with mock.patch.object(muse_llm.subprocess, "Popen", side_effect=OSError("cannot start")):
            code, stdout, stderr = self.run_muse(fake)
        self.assertEqual((code, stdout), (5, ""))
        self.assertIn("muse could not be started: cannot start", stderr)

    def test_muse_max_steps_must_be_positive(self):
        for value in ("0", "-1"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        muse_llm.parser().parse_args(["--max-steps", value])
                self.assertEqual(raised.exception.code, 2)

    def test_find_muse_skips_current_directory_and_honours_path(self):
        current = self.root / "muse-current"
        path_directory = self.root / "muse-path"
        current.mkdir()
        path_directory.mkdir()
        executable_name = "muse.cmd" if os.name == "nt" else "muse"
        for directory in (current, path_directory):
            executable = directory / executable_name
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
        original_cwd = Path.cwd()
        try:
            os.chdir(current)
            environment = {
                "PATH": os.pathsep.join(("", ".", str(current), str(path_directory))),
                "LOCALAPPDATA": str(self.root / "no-local-app-data"),
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            }
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(Path(muse_llm.find_muse()), path_directory / executable_name)
                self.assertEqual(Path(muse_llm.find_muse("muse")), path_directory / executable_name)
                self.assertEqual(Path(muse_llm.find_muse("./" + executable_name)), current / executable_name)
                self.assertIsNone(muse_llm.find_muse(str(current / "missing")))
        finally:
            os.chdir(original_cwd)

    @unittest.skipUnless(os.name == "nt", "Windows install fallback")
    def test_find_muse_uses_windows_install_fallback(self):
        local_app_data = self.root / "muse-local-app-data"
        executable = local_app_data / "Programs" / "muse" / "muse.cmd"
        executable.parent.mkdir(parents=True)
        executable.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PATH": "", "LOCALAPPDATA": str(local_app_data)}):
            self.assertEqual(Path(muse_llm.find_muse()), executable)
            self.assertIsNone(muse_llm.find_muse("missing-muse"))


if __name__ == "__main__":
    unittest.main()
