---
name: auto
description: Autonomously complete a task with a checkable goal by planning, delegating implementation to Codex, verifying, reviewing and looping until the goal holds, stopping with a written decision list when something needs the user. Use when the user says "auto", "just get it done", "run autonomously", "finish this while I am away", or hands over a goal and leaves.
---

# Auto

Take a goal and finish it without the user, or stop with a list of the decisions only they can make. Never guess through a fork; never take an irreversible or outward-facing action.

## Contract

The argument is a checkable condition, the same contract as `/goal`: a test count, an exit code, a file that exists, a status output, a command that prints an expected value. A goal given as prose or as a file path is turned into a checkable condition first, stated back in one line, and pursued under that reading. Two readings that would produce materially different work is a stop condition, not a coin flip.

Budget flags, given by the user or defaulted: `--max-iterations` (default 5) and `--max-minutes` (default 90). A hit budget is a stop condition.

## Loop

1. Plan once: read the repository, the newest handoff and the plan file for this project, decide the approach and the file list, and write the goal check as a command that exits 0 when the goal holds. Run it first; a goal that already holds ends the run.
2. Implement through the delegate skill: one brief per iteration, Codex implements, this skill never edits source itself. Follow-ups on the same task go through `codex exec resume`, never a fresh run.
3. Verify through the verify-pass skill on every Codex report. A refuted claim goes back through resume with the measurement attached.
4. Review: first mark every new file with `git add -N <path>` by explicit path so the runner's diff includes it (the runner diffs the working tree against the base and skips untracked files), then run the pr-review skill at tier 0 and tier 1 on the diff. Tier 2 runs only when tier 1 reports a finding or the diff touches auth, billing, a data path or a client repository. Confirmed findings go back through resume.
5. Run the goal check. Holds: go to Finish. Does not hold: next iteration, with the failing output in the brief.

Each iteration appends one row to `auto-trail.tsv` next to the newest handoff in the project (create it): iteration, what was done, why, evidence (the command and its observed value), result. Local only, never committed.

## Stop conditions

Any one of these ends the loop immediately and produces the decision list instead of another attempt:

- An action on the ask-first list of the user's CLAUDE.md: dropping tables, force pushes, history rewrites, deleting data, replaying into a live consumer, sending anything to a client or a shared channel, pushing to a remote, deploying, registering scheduled tasks or services, editing another machine.
- A permission denial from a hook or the classifier. Do not retry the denied action another way.
- A Codex quota exhaustion (exit 0 with no output file). Record the session id so the next session can resume it.
- The same finding failing after two resumes.
- A fork in the task that different readings would implement differently, or a fact the run needs that is not in the repository, the docs or the handoff.
- The iteration or minute budget.

## Finish

Whether the goal holds or a stop condition fired:

1. Commit on a branch this run created (`auto/<goal-slug>`), staging by explicit path, when the tree has changes that pass tier 0. Never push. On a client repository, commit only if the project CLAUDE.md allows local commits from agents; otherwise leave the tree and say so.
2. Run the save skill with the handoff trigger so a fresh session can resume.
3. Reply with: the goal and whether it holds (with the check's output), the iterations used, the branch and commit hash if any, and the decision list: one bullet per decision the user must make, each with the reason it is theirs in one sentence and the recommended option first.
