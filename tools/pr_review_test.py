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
        self.assertEqual(scorer.DEFAULT_PRICES["haiku"], (0.10, 0.50))
        self.assertAlmostEqual(scorer.review_cost(
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, scorer.DEFAULT_PRICES["haiku"]
        ), 0.60)

    def test_reviewer_override_replaces_default(self):
        override = runner.parse_override(f"agy={self.command(self.plain)}")
        configs = runner.load_reviewer_configs([override], home=self.home)
        self.assertEqual(configs["agy"]["cmd"], self.command(self.plain))
        self.assertTrue(configs["agy"]["stdin"])
        self.assertEqual(list(configs), ["agy", "codex", "haiku", "kimi", "muse"])

    def test_default_haiku_reviewer(self):
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "haiku")
        self.assertEqual(config, {
            "name": "haiku",
            "cmd": 'claude -p --model claude-haiku-5-5 --no-session-persistence --max-turns 1 --tools "" --append-system-prompt "Reply with the JSON findings array only, no prose."',
            "stdin": True,
        })
        tokens = runner.command_tokens(config["cmd"])
        self.assertEqual(tokens[tokens.index("--max-turns") + 1], "1")
        self.assertEqual(tokens[tokens.index("--tools") + 1], "")
        self.assertEqual(tokens[tokens.index("--append-system-prompt") + 1], "Reply with the JSON findings array only, no prose.")

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


    def test_default_muse_reviewer(self):
        config = next(item for item in runner.DEFAULT_REVIEWERS if item["name"] == "muse")
        self.assertTrue(config["stdin"])
        self.assertEqual(runner.command_tokens(config["cmd"]), [
            sys.executable, str(Path(runner.__file__).with_name("muse_llm.py")),
            "--model", "{muse_model}", "--effort", "{muse_effort}",
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
        self.assertEqual(list(configs), ["agy", "codex", "haiku", "kimi"])
        self.assertEqual(stderr.getvalue(), "pr-review: muse skipped (client machine, no diff leaves the machine)\n")

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
                self.assertEqual(configs["muse"], override)
                self.assertEqual(stderr.getvalue(), "")

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
                self.assertEqual(list(configs), ["agy", "codex", "haiku", "kimi"])
                self.assertEqual(stderr.getvalue(), "pr-review: muse skipped (machine.json unreadable)\n")
        override = runner.parse_override(f"muse={self.command(self.plain)}")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            configs = runner.load_reviewer_configs([override], home=self.home)
        self.assertEqual(configs["muse"], override)
        self.assertEqual(stderr.getvalue(), "")

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
        defaults = [item for item in runner.DEFAULT_REVIEWERS if item["name"] != "muse"]
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
                self.assertEqual(configs["muse"], override)
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
        completed = subprocess.CompletedProcess([], 0, stdout="\n".join(MUSE_FIXTURE_LINES), stderr="")
        with mock.patch.object(muse_llm.subprocess, "run", return_value=completed) as run:
            code, _, stderr = self.run_muse(fake)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(run.call_args.kwargs["timeout"], 240)

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
        with mock.patch.object(muse_llm.subprocess, "run", side_effect=OSError("cannot start")):
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
