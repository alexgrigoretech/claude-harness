# Iteration gate: the refine contract

Why this exists: "make it better" run a hundred times on a bad page produced two failure modes in the 2026-10-30 experiment. A careful model made tiny changes that went nowhere; a bold model added features until the page reached 21,000 lines and broke itself. Neither looked anything up. A gate outside the model, with budgets and latches, stopped the bold run at round 12. Refine mode runs under that gate.

## Contract

1. Subtract or refine only. A refine round removes a tell, fixes a finding, tightens spacing, corrects a color to a token, rewrites a line of copy. It never adds a section, a component, a state that was not in the brief, a dependency or a new token family. New scope needs a new brief through shape mode.
2. One review drives one round. A round starts from a findings list (the rubric's P0 to P3) and ends when those findings are fixed or refuted, not when the screen "feels done".
3. Budgets per round, default values, overridable in the project CLAUDE.md:
   - 120 changed lines across the round (added plus removed, as `git diff --stat` counts them)
   - 3 files touched
   - 1 render loop per fix batch, and one at the end of the round
   A round that would exceed a budget stops at the budget and reports what was left.
4. Latches. A check that passed once is latched: the checker exit 0 on a file, a rubric dimension at 4 or above, a tells count of zero on a region, the three widths rendering without overflow. A latched check that fails in a later round is a regression, not a finding: the round halts.
5. Round snapshots, no round commits. Round 1 starts only when the round's files carry no uncommitted edits from the user (those are committed or set aside by the user first, never by the skill); record `git rev-parse HEAD` as the starting point. Every round that ends passing is snapshotted, not committed: stage the round's new files by explicit path (`git add <files>`, so the snapshot includes them), run `git stash create`, and write the hash it prints in the round's line of the round log, so each passing state has a hash. The work is committed once, by the ship skill or on the user's ask (the commit rule in the global CLAUDE.md Git section).
6. Halt and rollback. On a regression, or a budget overrun with a failing checker, restore the round's files from the last passing round's snapshot (`git checkout <hash> -- <files>`, the round's files only, never anything else), leave the failing diff in the round log, and report the regression with the diff that caused it. With no passing round yet, the rollback target is the starting point recorded in step 5, and a file the round created is deleted.
7. Re-render and re-score every round. The scores are compared to the previous round; a round that lowers any dimension is rolled back even when the total went up.
8. Model by round. Shape and build are Fable or Opus. A refine round under this gate may run on a cheaper tier when the findings are P2 and P3 only, the budgets are set, and the round ends with a render that Fable or Opus reads before the result is reported. P0 and P1 fixes stay on Fable or Opus.
9. Stop after three rounds with no change in verdict. Three rounds that keep a fail is a signal that the direction, not the execution, is wrong; go back to shape.

## Round log

Each round appends one line to `.scratch/design-rounds.tsv` in the project (local, never committed): round number, findings addressed, lines changed, files touched, checker exit, rubric total, tells count, verdict, rolled back yes or no.

## What counts as a change

The budget counts source lines in UI files (styles, markup, components). DESIGN.md Decisions lines and the findings file do not count against it. A token added to DESIGN.md to replace a raw color counts as a refine, not as scope, provided it replaces a value that was already on the screen.
