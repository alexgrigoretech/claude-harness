# claude-harness

A shared setup for Claude Code and Codex CLI that installs into your home folder: one global CLAUDE.md with house rules, hooks that enforce the rules that must hold every time, and skills for the daily rituals (handoff, resume, ship, review, humanize). It came out of keeping the same setup identical on several personal and work machines from one private repository; this public repository is that setup with the private parts removed, released as a whole on every update.

## What you get

- `claude/CLAUDE.md`: the global rules (answer style, writing rules, how work is planned, verified and shipped, git rules). It imports two per-machine files from `~/.claude/local/`: `machine.md`, shipped here, and `machine.local.md`, written on your machine by `/setup` and never overwritten.
- Hooks in plain Python with no dependencies: a secret guard on file writes and shell commands, a draft wrap guard for Markdown drafts, a session context hook that prints the branch, the git identity in use and the newest handoff file at session start, a permission denial log, a context save nudge, a git commit-msg guard, and the optional Codex-first guard.
- Skills: `auto`, `delegate`, `humanizer`, `paste`, `pr-review`, `resume`, `save`, `setup`, `ship`, `status`, `verify-pass`. The Codex ones (`auto`, `delegate`, `verify-pass`, the Codex steps in `ship`) only matter if you run Codex CLI.
- Codex house rules (`codex/AGENTS.md`) with a config template, brief templates for implementation and verification, a writing checker (`checkers/check_writing.py`) and a pull request review runner (`tools/pr_review_runner.py`).

## Requirements

- Claude Code (CLI or desktop) and Python 3.11 or newer. Nothing is installed from PyPI.
- Optional: Codex CLI with a login, `gh`, `git`. `/setup` probes for them and asks.

## Install

```
git clone <this repository> ~/claude-harness
cd ~/claude-harness
python install.py --machine public --dry-run
python install.py --machine public
```

Read every line of the dry run before the real run: it lists each file the installer would write, back up or remove. The real run ends by running the hook test suite, which must pass. Use `python3` where `python` is not the right interpreter; the one that runs `install.py` is the one written into the hook commands. Where Claude Code's permission classifier blocks config edits under `~/.claude`, run the real install from inside a Claude session with the `!` prefix.

Then restart Claude Code and run `/setup`. It asks who you are (so the assistant calibrates its answers), your git identities and gh account, whether you use Codex CLI, and any project rules, and writes `machine.local.md` and `machine.local.json` under `~/.claude/local/`. No later install touches those two files.

## What the installer touches

- Copies `claude/CLAUDE.md` to `~/.claude/CLAUDE.md`, the hooks to `~/.claude/hooks/`, the skills to `~/.claude/skills/`, the templates to `~/.claude/templates/`, `codex/AGENTS.md` to `~/.codex/AGENTS.md`, and the machine files to `~/.claude/local/`. Previous versions go to `~/.claude/local/backup-<timestamp>/`.
- Merges into `~/.claude/settings.json` only these keys: the hook entries it owns under `PreToolUse`, `SessionStart`, `PermissionDenied` and `PostToolUse`, `permissions.allow`, `permissions.deny`, `permissions.ask`, `env.CLAUDE_CODE_SUBAGENT_MODEL` and `cleanupPeriodDays`. Everything else in the file stays as it is.
- Merges `claude/keybindings.json` into `~/.claude/keybindings.json`, keeping your own bindings.
- Removes nothing of yours. The delete list for this machine is empty, so a skill of your own under `~/.claude/skills/` survives an install; only a skill with one of the eleven names above is replaced.

`~/.codex/config.toml`, the status line and plugins stay yours, by hand. `codex/config.template.toml` is the reference.

## The Codex-first rule

The house rule is that Claude plans, hands implementation to Codex CLI with a written brief, and verifies the diff; a hook blocks direct edits to source files unless a named exception is written to a marker file. On this machine the hook is off by default. `/setup` asks whether Codex is installed and logged in; if it is, it records `"codex_first": true` in `~/.claude/local/machine.local.json` and the next `install.py` run turns the hook on. Without Codex, Claude edits directly and the rule does not apply.

## Addons

Some house tools stay out of the main install because they assume a project layout of their own. They ship as addon zips on this repository's Releases page, each with an `ADDON.md` that repeats the steps below.

- `daily`: type `daily` in a project and get one day of work summarized for a person who was not at the keyboard: a one minute status in plain words for the client or a manager, then milestones, successes, bottlenecks and insights for the person who did the work, and a training block that says what to do differently tomorrow, measured from git, handoff files, review logs, Codex run logs and the day's Claude Code session transcripts. `daily short` gives the status block alone.

Install: unzip the addon into the harness install folder (the clone), add `"daily": {"output": "<folder for the summaries>"}` to `~/.claude/local/machine.local.json`, run `python install.py --machine public` again and restart Claude Code. Update by unzipping a newer addon over the same folder and running the installer again.

## Updating

```
cd ~/claude-harness
git pull
python install.py --machine public
```

## Removing

Delete the hook entries in `~/.claude/settings.json` that point at `~/.claude/hooks/`, then remove `~/.claude/CLAUDE.md`, `~/.claude/hooks/`, the eleven skills above from `~/.claude/skills/`, `~/.claude/templates/`, `~/.claude/local/` and `~/.codex/AGENTS.md`. What the installer replaced is under `~/.claude/local/backup-<timestamp>/`.

## How this repository is produced

Every commit here is one release: a gate-checked bundle built from the private source and unzipped into this repository. Nothing is edited here by hand, so a pull request cannot survive the next release. Open an issue instead; a change that lands in the private source ships with the following release.

## License

MIT, see `LICENSE`. The humanizer skill carries material under other terms, listed in `THIRD-PARTY-NOTICES.md`.
