---
name: setup
description: 'Interview the person for the local context this machine still needs (who they are, git identities and gh account, Codex model, engagement rules), write ~/.claude/local/machine.local.md and machine.local.json, then apply them through install.py. Use when the user says "setup", "set up this machine", "first run", "onboard me", "who am I", or when the machine.local.md still carries the stub text after a bundle install.'
---

# Setup

The shared CLAUDE.md and the committed machine file travel in every bundle; everything about the person and the engagement lives in two local files no install overwrites. This skill fills them by asking, not by leaving placeholders. Ask everything the machine needs in as few rounds as possible, show the files before writing them, then apply.

## 1. Read what is there

Read `~/.claude/local/machine.md`, `~/.claude/local/machine.local.md`, `~/.claude/local/machine.json`, `~/.claude/local/machine-name` and `~/.claude/local/harness-mode`. When the local markdown already carries engagement bullets shipped in a bundle, ask only for the missing sections. Decide what is missing:

- the `## Who I am, for calibration` section is absent, or the file still opens with the installer's stub line "No local machine facts on this machine yet", or the section still carries any placeholder text ("Not written yet", "Replace this paragraph");
- `machine.json` has no `identities` map or no `gh_account`;
- no Codex line in the local markdown: neither a recorded model nor the statement that Codex is not used on this machine;
- no engagement or client rules in the local markdown.

If nothing is missing, say so in one line and offer a review; rewrite nothing that is complete unless asked.

## 2. Probe before asking

Run these first so the questions carry defaults instead of blanks:

```
git config --global user.name; git config --global user.email
gh auth status
codex --version
codex exec --skip-git-repo-check -m gpt-6-astra "reply with the single word ok" < /dev/null
python --version
```

Run the Codex probe through the Bash tool (Git Bash), not PowerShell, because PowerShell 5.1 rejects the `< /dev/null` redirection; the `--skip-git-repo-check` flag is there because a first-run session is usually not inside a git repository. The banner prints a `model:` line. Repeat with `gpt-5.6-sol` only when the error text names the model as unknown or unavailable to this login; any other failure (login, network, shell) is reported as such and no model is recorded. Read the interpreter that runs the hooks from the hook commands in `~/.claude/settings.json`; that is the interpreter install.py must run with.

## 3. Ask

Use AskUserQuestion for choices and plain questions for free text, grouped so the person answers in one or two rounds:

- Who you are: role, years, the domains where the assistant should skip textbook explanations, how you want to be told when you are wrong (default: plainly, with the reason, not softened).
- Git: the default commit email; every folder that commits under another identity, as folder and email pairs; the gh account name or names; whether to write the gitconfig includeIf blocks now or leave git as it is.
- Engagement: the client name (it stays in the local file and never enters a shared document), where its repos live, the data rules (default for a client machine: client data stays in client systems, samples in docs and tickets are synthesised or redacted, no personal connectors), and any review or PR rule the client imposes on top of the house rules.
- Codex: whether Codex CLI is installed and logged in (the probe says; a missing `codex` command or a login failure means no). If yes, confirm the probed model and version and the reasoning effort (default high). If no, record that Codex is not used here, so the Codex-first rule and its guard hook stay off; the person can run this skill again after installing Codex.
- Optional: the context save threshold (default from machine.json), MCP servers to add (context7 for library docs), anything else the person wants every session to know.

Never ask for or store a secret (token, password, key) in either file.

## 4. Write the two files

Show both before writing; they are short. When the local markdown already exists with engagement bullets, keep every existing bullet as it is and replace only the `## Who I am, for calibration` section (and add the Codex line if it is missing); do not rewrite bullets the bundle shipped.

`~/.claude/local/machine.local.md`: a title line, one bullet per fact (engagement and where its repos live, data rules, the git identity rule per folder, the Codex model and version or, without Codex, the line "Codex CLI is not set up on this machine: the Codex-first section of the global rules does not apply, Claude edits directly, and the delegate, auto and verify-pass skills are not used", MCP servers), then `## Who I am, for calibration` with two to four sentences in the person's own words and in the first person. One paragraph per line, no hard wrap, no em dashes or double dashes.

`~/.claude/local/machine.local.json`:

```
{
  "identities": {"default": "<email>", "<absolute folder with forward slashes>": "<email>"},
  "gh_account": "<account>",
  "codex_model": "<model that answered>",
  "codex_first": true
}
```

`codex_first` is true when Codex answered the probe and false when Codex is not used on this machine; omit `codex_model` in the false case. install.py reads the merged value: true installs the Codex-first guard hook, false leaves it out of the hook entries (the hook file is still copied). Add `"context_save_at": <tokens>` only when the person gave one. The commit-msg guard and the session context hook read `identities` from the installed `machine.json`; install.py merges this file into it.

## 5. Apply

From the install folder named in `machine.md`, with the interpreter from step 2 in place of `python`: `python install.py --dry-run`, read every line, then the real run (through the `!` prefix where the permission classifier blocks config edits under `~/.claude`). On the first run after writing the json the dry run lists `~/.claude/local/machine.json` as a write, and `~/.claude/settings.json` too when the Codex answer changed the hook entries; `machine.local.md` is reported unchanged because the installer keeps the person's section, and a re-run with nothing new reports both unchanged.

If the person agreed to git changes: `git config --global user.email <default>`; per folder, `git config --global includeIf."gitdir:<folder>/".path ~/.gitconfig-<label>` and a two-line `[user]` file at that path with the folder's email; then `gh auth switch --user <account>`. Confirm inside a repo of that folder with `git config user.email`. Never set a repo-local email and never pass `-c user.email`.

## 6. Report

Five lines or fewer: the two paths written, the identities as folder to email, the Codex model recorded or the statement that Codex is off here and the guard with it, what install.py changed, what was skipped and why. A restart of Claude Code picks up the new CLAUDE.md import.
