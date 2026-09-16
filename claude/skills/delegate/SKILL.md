---
name: delegate
description: Hand an implementation task to Codex CLI with a written brief, run it, check the output file, and resume for fixes. Use when the user says "delegate", "send to codex", "codex this", "implement via codex", or when the Codex-first rule applies to a coding task.
---

# Delegate

The Codex-first rule: Claude plans, orchestrates and verifies; Codex implements. This skill is the delegation step.

1. Decision gate: say "Delegating to Codex" before the first code edit of the task.
2. Brief: copy `~/.claude/templates/codex-implementation-brief.md` into the session scratchpad as `brief-<topic>.md` and fill every section: files it may touch, exact behavior, constraints, acceptance criteria with the commands that prove them, and what done means. Paste measured numbers; never let Codex guess a value, a column name or a path. No client data, real identifiers or credentials in a brief. Bake verified version facts in; Codex has no docs lookup.
3. Model: read `codex_model` from `~/.claude/local/machine.json` (gpt-6-astra where the login has it, gpt-5.6-sol otherwise) and pass it explicitly. Confirm the banner prints it.
4. Run, from the Bash tool, with the brief on stdin and the output file in the scratchpad:
   `codex exec - -C <repo> -m <model> -c model_reasoning_effort="high" --sandbox workspace-write --output-last-message <scratch>/codex-<topic>-out.md < <scratch>/brief-<topic>.md > <scratch>/codex-<topic>-run.log 2>&1`
   Inline prompts close stdin (`< /dev/null`) because Codex appends piped stdin. Non-git folders need `--skip-git-repo-check`. Long runs go to the background with a long timeout; never pipe a long run through `tail`, it buffers.
5. Before reading the summary: the output file must exist. Exit 0 with no output file means the Codex quota ran out mid-run. The quota resets at a fixed local time on some logins and is a rolling window on others; the machine file says which. Record the session id from the banner, schedule a wakeup for the reset (ScheduleWakeup or a note in the handoff), and resume with `codex exec resume <id>` when it fires.
6. Read CHANGED FILES, VERIFIED and ASSUMPTIONS/RISKS. Each is a claim. Run the verify-pass skill or at least: `git diff --stat` against the allowed file list, re-run every VERIFIED command, and exercise the change. Codex has previously claimed edits that never landed, overwritten a file mid-flight, implemented a plausible neighbour of the spec, and reported tests it did not write.
7. Findings go back through `codex exec resume <id> -m <model> -c model_reasoning_effort="high" -c sandbox_mode="workspace-write" "<finding and the exact fix wanted>" < /dev/null > <scratch>/codex-<topic>-resume<N>.log 2>&1`, numbering the resumes from 1 so the log names stay in order (the daily collector reads them where the plan tools are installed), never through a fresh run and never by editing the code yourself. After two failed resumes, take over and say so.
8. Report: what Codex changed, which verification ran, the result, the model used.

Known Codex quirks: `--output-last-message` needs its directory to exist; sandbox temp dirs differ from the session scratchpad; orphan lock files after a killed run; PYTHONPATH is not inherited inside worktrees; background jobs get killed on low memory on some machines (run gates in the foreground where the machine file says so).
