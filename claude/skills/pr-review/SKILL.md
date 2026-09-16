---
name: pr-review
description: Run the tiered pull request review process. Use when the user says "pr", "pr review", "review this PR", "cheap review", "run the reviewers", "pre-push review", or asks for a pull request review before pushing.
---

# PR review

Run the tiers in order. Stop immediately when a deterministic gate fails and report its output. The ship skill calls this skill before it pushes.

## Tier 0: deterministic gates

1. Read the repository's `CLAUDE.md` and run its test and lint commands.
2. List changed documentation files. When there are any, run `python checkers/check_writing.py <changed docs>` from the repository if that checker exists, otherwise use `python <harness install folder>/checkers/check_writing.py <changed docs>`, where the install folder is the one named in `~/.claude/local/machine.md`. When the changed docs are the project's own prose (handoffs, plans, READMEs, skill files), run a second pass with `--rules hard-wrap` on those files (the option replaces the default set, so it is a separate run); never on vendored or upstream documents wrapped at 80 columns.
3. When `gitleaks` is installed, run it against the working tree. Treat every nonzero result from a required test, lint, writing, or secret gate as a stop condition.

## Tier 1: cheap reviewers (Gemini through agy, Codex read-only, Kimi where installed)

Before running the runner, mark new files with `git add -N <path>` by explicit path; the runner diffs the working tree against the base and would otherwise skip untracked files. Take the harness install folder from `~/.claude/local/machine.md` (the clone on the machine that holds it, the bundle folder elsewhere; `tools/` is present in both). Run `python <harness install folder>/tools/pr_review_runner.py --base <default branch>` with the interpreter the machine file names, then read `pr-review-findings.json`. The default reviewers are `agy` (Gemini 3.8 Flash at low effort through the Antigravity CLI wrapper `tools/agy_llm.py`, about 5 s a diff) and `codex` (gpt-5.6-sol, `--sandbox read-only`, about a minute a diff); `kimi` runs where the Kimi CLI is installed. Each reviewer sees only the diff and the body, so expect claims about symbols defined outside the diff (a missing import that sits on line 1, for example); tier 2 refutes those. A reviewer failure does not invalidate successful reviewer output, but no successful reviewer is a stop condition for this tier.

## Tier 2: verification review (Claude)

Model rule (2026-09-14): tier 2 runs on Fable, the session model, because this tier is the last gate before push and the one that refutes cheap claims. Fall back to Opus only when the usage window is blocking work, and then only for diffs under about 300 lines that touch no auth, billing, data path or client repository, and where agy and Codex agree. `/code-review` runs on the session model, so an Opus review means a session started with `/model opus` (never saved on a host that runs headless jobs) or a subagent with a model override; say which model reviewed in the report.

Run `/code-review high` once. Paste every tier 1 finding under the heading `claims to verify or refute`, each with its reviewer name. The resulting review must label every cheap finding as confirmed, refuted, or out of scope, and must include any independent findings from the high-confidence review.

## Tier 3: feedback to the implementer

Send the confirmed findings back to the session that produced the change: `codex exec resume <session id>` with the findings as the brief (file, line, claim, evidence, and what "fixed" means for each). Re-run tier 0 after every fix cycle. Repeat tier 1 and tier 2 when a fix materially changes the reviewed logic. Report to the user per reviewer: findings, confirmed, refuted, and what was fixed.

After each real run, append one line to `~/.claude/plans/harness-pr-review-log.md` containing the date, repository, reviewer names, finding count, confirmed count, and refuted count. Keep the log free of diff content and client details so the decision after 10 pull requests has usable aggregate data.
