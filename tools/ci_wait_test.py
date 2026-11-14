import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import ci_wait


def check(bucket, name="build", workflow="CI", link="https://example.test/checks/1"):
    return {"name": name, "state": "IGNORED", "bucket": bucket, "link": link, "workflow": workflow}


def poll(checks, code=0, stderr=""):
    return subprocess.CompletedProcess([], code, json.dumps(checks), stderr)


def error(code=1, stderr="connection failed\nextra detail", stdout=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FakeTime:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class CiWaitTests(unittest.TestCase):
    def run_wait(self, polls, argv=None, ghx=False):
        timer = FakeTime()
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(ci_wait.Path, "is_file", return_value=ghx), \
                mock.patch.object(ci_wait.subprocess, "run", side_effect=polls) as run, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = ci_wait.main(argv or [], clock=timer.clock, sleep=timer.sleep)
        return code, output.getvalue(), errors.getvalue(), timer, run

    def test_all_pass_first_poll(self):
        code, output, errors, timer, run = self.run_wait([poll([check("pass")])], ["12"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "waiting on 1 checks for 12\n1 passed, 0 failed, 0 pending, 0 skipped after 0s\n")
        self.assertEqual(errors, "")
        self.assertEqual(timer.sleeps, [])
        self.assertEqual(run.call_count, 1)

    def test_quiet_polls_and_only_changed_buckets(self):
        pending = [check("pending"), check("pass", name="lint")]
        passed = [check("pass", name="lint"), check("pass")]
        code, output, errors, timer, run = self.run_wait([
            poll(pending, 8), poll(pending, 8), poll(passed),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), [
            "waiting on 2 checks for current branch", "build: pending -> pass",
            "2 passed, 0 failed, 0 pending, 0 skipped after 60s",
        ])
        self.assertEqual(errors, "")
        self.assertEqual(timer.sleeps, [30, 30])
        self.assertEqual(run.call_count, 3)

    def test_failure_is_immediate_with_pending_checks(self):
        code, output, errors, timer, run = self.run_wait([
            poll([check("fail"), check("pending", name="lint")], 1),
        ])
        self.assertEqual(code, 1)
        self.assertIn("0 passed, 1 failed, 1 pending, 0 skipped after 0s", output)
        self.assertIn("build: https://example.test/checks/1\n", output)
        self.assertEqual(timer.sleeps, [])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(errors, "")

    def test_cancel_is_failure(self):
        code, output, _, timer, _ = self.run_wait([poll([check("cancel")], 1)])
        self.assertEqual(code, 1)
        self.assertIn("1 failed", output)
        self.assertIn("build: https://example.test/checks/1", output)
        self.assertEqual(timer.sleeps, [])

    def test_skipping_settles(self):
        code, output, _, timer, _ = self.run_wait([poll([check("skipping")])])
        self.assertEqual(code, 0)
        self.assertIn("0 passed, 0 failed, 0 pending, 1 skipped after 0s", output)
        self.assertEqual(timer.sleeps, [])

    def test_timeout_caps_last_sleep(self):
        code, output, _, timer, run = self.run_wait(
            [poll([check("pending")], 8)] * 3, ["--timeout", "35"],
        )
        self.assertEqual(code, 2)
        self.assertEqual(timer.sleeps, [30, 5])
        self.assertEqual(timer.now, 35)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(output.splitlines(), [
            "waiting on 1 checks for current branch",
            "0 passed, 0 failed, 1 pending, 0 skipped after 35s",
        ])

    def test_timeout_without_checks_before_grace(self):
        cases = (
            ([poll([]), poll([])], 0),
            ([error(stderr="no checks reported")] * 2, 0),
            ([error(), error()], 0),
            ([poll([]), error()], 0),
            ([poll([check("pending")], 8), poll([])], 1),
        )
        for index, (polls, initial_count) in enumerate(cases):
            with self.subTest(case=index):
                code, output, _, timer, run = self.run_wait(polls, ["--timeout", "30"])
                self.assertEqual(code, 5)
                self.assertEqual(output.splitlines(), [
                    f"waiting on {initial_count} checks for current branch",
                    "ci_wait: no checks reported after 30s",
                ])
                self.assertEqual(timer.sleeps, [30])
                self.assertEqual(run.call_count, 2)

    def test_no_checks_then_checks_appear(self):
        code, output, errors, timer, _ = self.run_wait([
            error(stderr="no checks reported"), poll([]), poll([check("pass")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), [
            "waiting on 0 checks for current branch",
            "1 passed, 0 failed, 0 pending, 0 skipped after 60s",
        ])
        self.assertEqual(errors, "")
        self.assertEqual(timer.sleeps, [30, 30])

    def test_no_checks_past_grace(self):
        for empty in (poll([]), error(stderr="no checks reported")):
            with self.subTest(stdout=empty.stdout):
                code, output, errors, timer, run = self.run_wait([empty] * 4)
                self.assertEqual(code, 5)
                self.assertEqual(output.splitlines(), [
                    "waiting on 0 checks for current branch", "ci_wait: no checks reported after 90s",
                ])
                self.assertEqual(errors, "")
                self.assertEqual(timer.now, 90)
                self.assertEqual(run.call_count, 4)

    def test_three_transient_errors(self):
        for stdout in ("", "{}", "null", "invalid json"):
            with self.subTest(stdout=stdout):
                code, output, errors, timer, run = self.run_wait([error(7, stdout=stdout)] * 3)
                self.assertEqual(code, 3)
                self.assertEqual(errors, "ci_wait: gh failed (exit 7): connection failed\n" * 3)
                self.assertEqual(output, "waiting on 0 checks for current branch\n")
                self.assertEqual(timer.sleeps, [30, 30])
                self.assertEqual(run.call_count, 3)

    def test_two_errors_then_good_poll(self):
        code, output, errors, _, _ = self.run_wait([error(), error(), poll([check("pass")])])
        self.assertEqual(code, 0)
        self.assertEqual(len(errors.splitlines()), 2)
        self.assertIn("1 passed", output)

    def test_good_poll_resets_error_streak(self):
        code, _, errors, _, run = self.run_wait([
            error(), error(), poll([check("pending")], 8),
            error(), error(), poll([check("pass")]),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(errors.splitlines()), 4)
        self.assertEqual(run.call_count, 6)

    def test_no_checks_resets_error_streak(self):
        code, _, _, _, _ = self.run_wait([
            error(), error(), error(stderr="no checks reported"), error(), error(), poll([check("pass")]),
        ], ["--grace", "300"])
        self.assertEqual(code, 0)

    def test_json_list_is_used_regardless_of_exit_code(self):
        for status in (0, 1, 8, 7):
            with self.subTest(status=status):
                code, output, errors, _, _ = self.run_wait([poll([check("pass")], status, "ignored")])
                self.assertEqual(code, 0)
                self.assertIn("1 passed", output)
                self.assertEqual(errors, "")

    def test_duplicate_names_have_workflow_prefix(self):
        code, output, _, _, _ = self.run_wait([
            poll([check("pending", workflow="CI"), check("pending", workflow="Release")], 8),
            poll([check("pass", workflow="Release"), check("fail", workflow="CI")], 1),
        ])
        self.assertEqual(code, 1)
        self.assertEqual(output.splitlines(), [
            "waiting on 2 checks for current branch", "Release / build: pending -> pass",
            "CI / build: pending -> fail", "1 passed, 1 failed, 0 pending, 0 skipped after 30s",
            "CI / build: https://example.test/checks/1",
        ])

    def test_ghx_command_with_user_and_cwd(self):
        code, _, _, _, run = self.run_wait([poll([check("pass")])], [
            "12", "--repo", "acme/widgets", "--user", "octo-one", "--cwd", "repo-dir",
        ], ghx=True)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [
            sys.executable, str(Path(ci_wait.__file__).resolve().with_name("ghx.py")),
            "--user", "octo-one", "--cwd", "repo-dir", "pr", "checks", "12",
            "--repo", "acme/widgets", "--json", "name,state,bucket,link,workflow",
        ])
        self.assertIsNone(run.call_args.kwargs["cwd"])

    def test_user_without_ghx_is_error(self):
        code, output, errors, timer, run = self.run_wait([], ["--user", "octo-one"])
        self.assertEqual(code, 6)
        self.assertEqual(output, "")
        self.assertEqual(errors, "ci_wait: --user needs ghx.py next to this tool\n")
        self.assertEqual(timer.sleeps, [])
        run.assert_not_called()

    def test_direct_gh_uses_cwd_and_preserves_pr(self):
        for pr in ("12", "https://example.test/acme/widgets/pull/12", "topic/checks"):
            with self.subTest(pr=pr):
                code, _, _, _, run = self.run_wait([poll([check("pass")])], [
                    pr, "--repo", "acme/widgets", "--cwd", "repo-dir",
                ])
                self.assertEqual(code, 0)
                self.assertEqual(run.call_args.args[0], [
                    "gh", "pr", "checks", pr, "--repo", "acme/widgets",
                    "--json", "name,state,bucket,link,workflow",
                ])
                self.assertEqual(run.call_args.kwargs["cwd"], "repo-dir")

    def test_default_command_omits_pr(self):
        _, _, _, _, run = self.run_wait([poll([check("pass")])])
        self.assertEqual(run.call_args.args[0], ["gh", "pr", "checks", "--json", "name,state,bucket,link,workflow"])

    def test_gh_missing(self):
        code, output, errors, timer, _ = self.run_wait([FileNotFoundError()])
        self.assertEqual(code, 127)
        self.assertEqual(output, "")
        self.assertEqual(errors, "ci_wait: gh not found on PATH\n")
        self.assertEqual(timer.sleeps, [])

    def test_interrupt_during_poll_or_sleep(self):
        code, output, errors, _, _ = self.run_wait([KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        self.assertEqual(errors, "")
        with mock.patch.object(ci_wait.Path, "is_file", return_value=False), \
                mock.patch.object(ci_wait.subprocess, "run", return_value=poll([check("pending")], 8)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            code = ci_wait.main([], clock=lambda: 0, sleep=mock.Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(code, 130)
        self.assertEqual(errors.getvalue(), "")

    def test_defaults_and_help(self):
        args = ci_wait.parse_args([])
        self.assertEqual((args.timeout, args.interval, args.grace), (3600, 30, 90))
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
            ci_wait.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        self.assertIn("Exit codes: 0 passed, 1 failed, 2 timed out while pending, 3 gh errors, "
                      "5 no checks, 6 usage, 127 gh missing, 130 interrupted", help_text)
        self.assertIn("6 usage", help_text)

    def test_invalid_timing_arguments(self):
        for option, value in (("--interval", "0"), ("--timeout", "-1"), ("--grace", "-1"),
                              ("--interval", "nan"), ("--timeout", "inf")):
            with self.subTest(option=option, value=value), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                ci_wait.parse_args([option, value])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
