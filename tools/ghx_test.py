import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghx


TOKEN = "synthetic-token-for-test"


class GhxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.machine = self.home / ".claude" / "local" / "machine.json"
        self.machine.parent.mkdir(parents=True)
        self.environment = {"CLAUDE_HOOKS_HOME": str(self.home), "KEPT": "value"}
        patcher = mock.patch.dict(os.environ, self.environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ghx.shutil, "which", return_value="/bin/gh")
        self.which = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ghx.subprocess, "run")
        self.run = patcher.start()
        self.addCleanup(patcher.stop)
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, TOKEN + "\n", ""),
            subprocess.CompletedProcess([], 0),
        ]

    def configure(self, value):
        self.machine.write_text(json.dumps(value), encoding="utf-8-sig")

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ghx.main(list(arguments))
        self.assertNotIn(TOKEN, stdout.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def test_explicit_user_overrides_map(self):
        self.configure({"gh_accounts": {"default": "octo-one"}})
        self.assertEqual(self.invoke("--user", "octo-two", "pr", "view", "1"), (0, "", ""))
        self.assertEqual(self.run.call_args_list[0].args[0], [
            "/bin/gh", "auth", "token", "--user", "octo-two",
        ])

    @mock.patch.object(ghx.Path, "is_dir", return_value=True)
    def test_print_user_resolution_paths(self, is_dir):
        cases = [
            ({"gh_accounts": {"default": "octo-one"}}, "/work/alpha", ["--user", "octo-two"], "octo-two"),
            ({"gh_accounts": {"/work": "octo-one", "/work/alpha": "octo-two"}}, "/work/alpha/sub", [], "octo-two"),
            ({"gh_accounts": {"/work/alpha": "octo-one"}}, "/work/alpha", [], "octo-one"),
            ({"gh_accounts": {"/work/alpha": "octo-one"}}, "/work/alpha/sub", [], "octo-one"),
            ({"gh_accounts": {"/work/alpha": "octo-two", "default": "octo-one"}}, "/work/other", [], "octo-one"),
            ({"gh_accounts": {"/work/alpha": "octo-two"}, "gh_account": "octo-one"}, "/work/other", [], "octo-one"),
            ({"gh_account": "octo-one"}, "/work/other", [], "octo-one"),
            ({"gh_accounts": {"/work/alpha": "octo-two", "default": "octo-one"}, "gh_account": "octo-two"}, "/work/other", [], "octo-one"),
            ({"gh_accounts": {"/work/alpha": "octo-two", "default": "octo-one"}}, "/work/alphabeta", [], "octo-one"),
            ({"gh_accounts": {}}, "/work/alpha", [], "active"),
            ({}, "/work/alpha", [], "active"),
        ]
        for machine, cwd, arguments, expected in cases:
            with self.subTest(machine=machine, cwd=cwd, arguments=arguments):
                self.configure(machine)
                self.assertEqual(self.invoke(*arguments, "--cwd", cwd, "--print-user"), (0, expected + "\n", ""))
        self.run.assert_not_called()
        self.which.assert_not_called()

    @mock.patch.object(ghx.Path, "is_dir", return_value=True)
    def test_slashes_trailing_separator_and_windows_case(self, is_dir):
        self.configure({"gh_accounts": {"/work/alpha/": "octo-one"}})
        self.assertEqual(self.invoke("--cwd", "\\work\\alpha\\sub\\", "--print-user"), (0, "octo-one\n", ""))
        if os.name == "nt":
            self.configure({"gh_accounts": {"C:\\Work\\Alpha\\": "octo-two"}})
            self.assertEqual(self.invoke("--cwd", "c:/work/ALPHA/sub/", "--print-user"), (0, "octo-two\n", ""))
        else:
            self.configure({"gh_accounts": {"/Work/Alpha": "octo-one"}})
            self.assertEqual(self.invoke("--cwd", "/work/alpha", "--print-user"), (0, "active\n", ""))

    def test_home_prefix_and_process_cwd(self):
        self.configure({"gh_accounts": {"~/work": "octo-one"}})
        with mock.patch.object(ghx.os, "getcwd", return_value=str(self.home / "work" / "alpha")):
            self.assertEqual(self.invoke("--print-user"), (0, "octo-one\n", ""))
        self.configure({"gh_accounts": {"~": "octo-two"}})
        self.assertEqual(self.invoke("--cwd", str(self.home), "--print-user"), (0, "octo-two\n", ""))

    def test_missing_machine_prints_active(self):
        self.assertEqual(self.invoke("--print-user"), (0, "active\n", ""))
        self.run.assert_not_called()

    def test_no_map_inherits_environment(self):
        os.environ.update({"GH_TOKEN": "existing", "GITHUB_TOKEN": "existing-other"})
        original = dict(os.environ)
        self.run.side_effect = [subprocess.CompletedProcess([], 0)]
        self.assertEqual(self.invoke("pr", "view", "1"), (0, "", ""))
        self.run.assert_called_once_with(["/bin/gh", "pr", "view", "1"], cwd=None, env=None, check=False)
        self.assertEqual(dict(os.environ), original)

    def test_child_environment_streams_arguments_and_pending_exit(self):
        os.environ.update({"GH_TOKEN": "existing", "GITHUB_TOKEN": "existing-other"})
        original = dict(os.environ)
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, "  " + TOKEN + "\n", ""),
            subprocess.CompletedProcess([], 8),
        ]
        arguments = ["pr", "checks", "1", "--user", "octo-two", "--json", "state"]
        cwd = str(self.home)
        self.assertEqual(self.invoke("--user", "octo-one", "--cwd", cwd, *arguments), (8, "", ""))
        self.assertEqual(self.run.call_args.args[0], ["/bin/gh", *arguments])
        self.assertEqual(self.run.call_args.kwargs, {
            "cwd": cwd, "env": {**self.environment, "GH_TOKEN": TOKEN}, "check": False,
        })
        self.assertEqual(dict(os.environ), original)

    def test_explicit_separator_preserves_gh_options(self):
        self.assertEqual(self.invoke("--user", "octo-one", "--", "--user", "octo-two", "pr", "list"), (0, "", ""))
        self.assertEqual(self.run.call_args.args[0], ["/bin/gh", "--user", "octo-two", "pr", "list"])

    def test_host_is_used_for_token_lookup(self):
        os.environ["GH_HOST"] = "git.example.com"
        for public_tokens in ({}, {"GH_TOKEN": "existing", "GITHUB_TOKEN": "existing-other"}):
            with self.subTest(public_tokens=bool(public_tokens)):
                os.environ.update({
                    **public_tokens, "GH_ENTERPRISE_TOKEN": "existing-enterprise",
                    "GITHUB_ENTERPRISE_TOKEN": "existing-enterprise-other",
                })
                original = dict(os.environ)
                self.run.reset_mock()
                self.run.side_effect = [
                    subprocess.CompletedProcess([], 0, TOKEN + "\n", ""),
                    subprocess.CompletedProcess([], 0),
                ]
                self.assertEqual(self.invoke("--user", "octo-one", "pr", "list"), (0, "", ""))
                self.assertEqual(self.run.call_args_list[0].args[0], [
                    "/bin/gh", "auth", "token", "--user", "octo-one", "--hostname", "git.example.com",
                ])
                expected = {**original, "GH_ENTERPRISE_TOKEN": TOKEN}
                expected.pop("GITHUB_ENTERPRISE_TOKEN")
                self.assertEqual(self.run.call_args.kwargs["env"], expected)
                self.assertEqual(dict(os.environ), original)

    def test_github_host_uses_public_token(self):
        os.environ.update({"GH_HOST": "github.com", "GITHUB_TOKEN": "existing-other"})
        self.assertEqual(self.invoke("--user", "octo-one", "pr", "list"), (0, "", ""))
        self.assertEqual(self.run.call_args_list[0].args[0], [
            "/bin/gh", "auth", "token", "--user", "octo-one", "--hostname", "github.com",
        ])
        self.assertEqual(self.run.call_args.kwargs["env"], {
            **self.environment, "GH_HOST": "github.com", "GH_TOKEN": TOKEN,
        })

    def test_ghe_host_uses_public_token(self):
        os.environ.update({"GH_HOST": "octo.ghe.com", "GITHUB_TOKEN": "existing-other"})
        self.assertEqual(self.invoke("--user", "octo-one", "pr", "list"), (0, "", ""))
        self.assertEqual(self.run.call_args_list[0].args[0], [
            "/bin/gh", "auth", "token", "--user", "octo-one", "--hostname", "octo.ghe.com",
        ])
        self.assertEqual(self.run.call_args.kwargs["env"], {
            **self.environment, "GH_HOST": "octo.ghe.com", "GH_TOKEN": TOKEN,
        })

    def test_missing_cwd_runs_nothing(self):
        missing = str(self.home / "missing")
        self.configure({})
        for cwd in (missing, str(self.machine)):
            for arguments in (["pr", "list"], ["--print-user"], ["--user", "octo-one", "pr", "list"]):
                with self.subTest(cwd=cwd, arguments=arguments):
                    with mock.patch.object(ghx, "load_machine") as load_machine:
                        self.assertEqual(self.invoke("--cwd", cwd, *arguments), (
                            2, "", f"ghx: no such folder: {cwd}\n",
                        ))
                        load_machine.assert_not_called()
        self.run.assert_not_called()
        self.which.assert_not_called()

    def test_token_failures_are_private(self):
        failures = [
            subprocess.CompletedProcess([], 1, TOKEN, TOKEN),
            subprocess.CompletedProcess([], 0, " \n", TOKEN),
            OSError(TOKEN),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.run.reset_mock()
                self.run.side_effect = [failure]
                self.assertEqual(self.invoke("--user", "octo-one", "pr", "list"), (
                    4, "", "ghx: no stored token for octo-one; run gh auth login for that account\n",
                ))
                self.assertEqual(self.run.call_count, 1)

    def test_token_timeout_has_private_diagnostic(self):
        command = ["/bin/gh", "auth", "token", "--user", "octo-one"]
        self.run.side_effect = subprocess.TimeoutExpired(command, 10, output=TOKEN, stderr=TOKEN)
        code, stdout, stderr = self.invoke("--user", "octo-one", "pr", "list")
        self.assertEqual((code, stdout, stderr), (
            4, "", "ghx: gh auth token timed out for octo-one; the credential store did not answer in 10 seconds\n",
        ))
        self.assertNotIn("run gh auth login", stderr)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0], command)

    def test_child_errors_do_not_expose_token(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 0, TOKEN, ""), OSError(TOKEN)]
        self.assertEqual(self.invoke("--user", "octo-one", "pr", "list"), (1, "", "ghx: cannot run gh\n"))

    def test_missing_gh(self):
        self.which.return_value = None
        self.assertEqual(self.invoke("pr", "list"), (127, "", "ghx: gh not found on PATH\n"))
        self.run.assert_not_called()

    def test_gh_disappears_after_path_lookup(self):
        for arguments in (["pr", "list"], ["--user", "octo-one", "pr", "list"]):
            with self.subTest(arguments=arguments):
                self.run.side_effect = FileNotFoundError(TOKEN)
                self.assertEqual(self.invoke(*arguments), (127, "", "ghx: gh not found on PATH\n"))

    def test_usage(self):
        for arguments in ([], ["--user"], ["--cwd"], ["--user="], ["--cwd=", "pr"], ["--user", "--print-user"], ["--"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.invoke(*arguments), (2, "", ghx.USAGE + "\n"))
        self.run.assert_not_called()

    def test_equals_options(self):
        self.assertEqual(self.invoke("--user=octo-one", f"--cwd={self.home}", "--print-user"), (0, "octo-one\n", ""))

    def test_invalid_configuration_has_private_error(self):
        self.machine.write_text(TOKEN, encoding="utf-8")
        self.assertEqual(self.invoke("pr", "list"), (2, "", "ghx: cannot read account configuration\n"))
        self.run.assert_not_called()
        self.assertEqual(self.invoke("--user", "octo-one", "--print-user"), (0, "octo-one\n", ""))


if __name__ == "__main__":
    unittest.main()
