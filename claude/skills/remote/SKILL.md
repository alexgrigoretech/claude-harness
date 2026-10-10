---
name: remote
description: 'Run a prompt on another machine through claude -p over ssh, or collect a detached run. Use when the user says "remote <machine>", "run this on <machine>", "ask <machine>", or a per machine skill is missing.'
---

# /remote

Find the harness install folder in `~/.claude/local/machine.md`. The tool is `<harness install folder>/tools/remote_prompt.py`. Use the configured machine name as `<name>` below and quote the tool path when it contains spaces. The argument is the prompt; it may start with a slash command such as `/auto <goal>`, which the remote session expands itself.

1. Write the argument verbatim to `<scratchpad>/remote-<name>-<timestamp>.md` (the session scratchpad, never the project).
2. Short prompts (a question, a status, a one step task): run `python <tool path> <name> --prompt-file <that file>` from the Bash tool and relay the stdout answer; the stderr footer carries the session id, turns, duration and cost; a `permission denied` block in stderr means the remote session was blocked and the user must decide.
3. Long prompts (anything starting with `/auto`, `/ship`, or a task that would run Codex): add `--detach`, print the run id, then poll `python <tool path> <name> --collect` no more than once every five minutes (Monitor or a background Bash loop), and relay the result when it arrives. Exit 4 means still running. Exit 3 means finished without a result. Use `--collect <run>` for a specific run, or `--runs` to list runs.
4. Follow-ups in the same remote conversation: add `--continue`. Collect a detached run first; `--continue` refuses an uncollected run. Use `--resume <session id>` for an explicit session instead.
5. `--cwd <folder>` runs in another folder on that machine; `--dry-run` shows the exact command without running it. For an ssh entry, from Git Bash prefix the command with `MSYS_NO_PATHCONV=1` and quote the value, such as `--cwd '/srv/app'` or `--cwd '~/x'`. This disables path conversion for every argument, so give `--prompt-file` and `--machine-json` values as Windows paths such as `C:/Users/...` or `F:/...`, not `/c/Users/...` forms, to avoid a `No such file or directory` error.

An ssh entry must point at a machine whose login shell is POSIX; a Windows host over ssh is not supported.

Never put the prompt on the command line; always use the prompt file. Never run two detached runs on the same machine at once without the user asking.

The merged `~/.claude/local/machine.json` has a top level `remote` object with this shape:

```json
{
  "remote": {
    "<name>": {
      "ssh": "<ssh config alias>",
      "shell": "zsh -ic",
      "cwd": "~",
      "claude": "claude",
      "permission_mode": "auto",
      "timeout": 600
    }
  }
}
```

Replace the placeholders with a configured alias and a machine name matching `^[a-z][a-z0-9-]{0,31}$`. Allowed entry keys are `ssh`, `shell`, `cwd`, `claude`, `permission_mode`, `max_turns` and `timeout`. All are optional. Without `ssh`, the prompt runs on this computer; `shell` requires `ssh`. The default folder is `~` over ssh and the current directory locally. The default executable is `claude`, and the default timeout is 600 seconds. `max_turns` and `timeout` must be positive integers. Permission modes are `default`, `acceptEdits`, `plan`, `auto`, `dontAsk` and `bypassPermissions`; when absent, no permission mode flag is passed. The tool accepts `--permission-mode`, `--max-turns`, `--max-budget-usd` and `--timeout` for a single run.

Edit the machine's local json and run install.py to generate a `/<name>` skill for each entry. Names must not match repository skill directories. The installer refuses to overwrite a skill it did not generate, and it does not delete skills for removed names. Sessions and the last detached run for each machine are recorded in `~/.claude/local/remote-sessions.json`.
