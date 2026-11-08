---
name: polish
description: Alternate a code review and a simplify pass over the current change for a capped number of rounds, later rounds scoped to what the previous round changed. Use when the user says "polish", "code review and simplify", "simplify and code review", "3 rounds", "N rounds auto", "do one more round of simplify and code review", or asks for review and cleanup rounds on a branch.
---

# Polish

Replaces the retyped "code review and simplify 3 rounds auto". The argument is the round count; the default is 3, and 5 is allowed only when the user names the change as high risk (auth, billing, a data path, a client repository). Never more than 5: past that point reviews start trading hypothetical edge cases for scope creep.

Before round 1: record the base as the branch point against the default branch (`git merge-base HEAD <default branch>`), so committed branch work is reviewed too; only on the default branch itself with uncommitted work is the base `HEAD`. Record the diff size. The review target of round 1 is the whole change against that base, committed and uncommitted.

Each round:
1. `/code-review medium` on the round's target. Every finding is fixed through the delegate skill's resume step on the implementer session that produced the change (a fresh delegate run when there was none, the trivial exception when it applies and is named). A finding that fails after two resumes stops the loop and goes to the report.
2. `/simplify` on the same target; its fixes land the same way.
3. Gate: run the project's test and lint commands from its CLAUDE.md. A failing gate is fixed in this round before the next starts.
4. Round record: append one line to `auto-trail.tsv` next to the newest handoff, or at the repository root when there is no handoff, with the auto skill's columns (step, what, why, evidence, result) so one collector reads both.
5. Snapshot and scope: at the start of every round take a snapshot of the working tree that survives without a commit, `git stash create` (prints a SHA and changes nothing) or, when it prints nothing, `git rev-parse HEAD`. The next round's target is `git diff <previous round's snapshot>`, which is exactly what the previous round changed, plus the findings still open. Unchanged files are not re-reviewed.

Stop early when a round ends with zero findings from both passes and a green gate: more rounds on an unchanged diff produce invented findings. Stop at the cap otherwise.

Report in five lines: rounds run, findings found and fixed per round, open findings with their reason, gate result, and whether the change is ready for the ship or merge skill. Never push, never commit in this skill unless the user's instruction included it.
