---
name: pr-brief
description: Write the one-page A4 PDF brief that accompanies every pull request, from the branch's diff, commits, review findings and plan status. Use when the user says "pr brief", "brief for the PR", "the one pager", "write the PDF for the PR", or after a PR exists in the ship or merge skills. Not for the PR body itself, which the ship skill writes.
---

# PR brief

One page, A4, for the person who merges without reading the diff: what was achieved, how it works in one diagram, what to know at the big picture level, and progress as a percent of the task and of the long term plan. Local only: the HTML and the PDF live in `~/.claude/plans/pr-briefs/` and are never committed, never bundled and never attached to the PR.

The page is design content, so it is written by Fable or Opus (the session model when it is one of those, otherwise an Opus agent), never by Codex, Sonnet or Haiku.

## Gather

1. The branch against its base: `git log --oneline <base>..<branch>` and `git diff --stat <base>..<branch>`. On a stacked branch, the base is the previous branch of the stack, so the brief describes only this PR's own commits.
2. The review: the findings from the pr-review or code-review run on this diff and what was done with each (fixed, refuted, out of scope). A brief for a change that was never reviewed says so in the know section.
3. The plan counts. Where the plan tools are installed, `python tools/masterplan.py table` from the repository gives the decisions and their status; count done over live decisions, with rejected excluded and partial as half. Otherwise use the checklist in `docs/PLAN.md`. Where neither exists, the task count only. Always write the arithmetic next to the number, and show a partial both ways (counted as half, and as not done).
4. The task: how many pieces the task named in the handoff or plan has, which are merged, which this PR delivers, and the percent with and without this PR counted.
5. Tests: the suite counts the gate printed, pasted, never estimated.

## Write

1. Copy `template.html` from this skill's folder to `~/.claude/plans/pr-briefs/<repo>-pr<n>.html`. Before a PR number exists, name it after the branch (`<repo>-<branch-slug>.html`) and copy it to the numbered name once the PR is open.
2. Fill every placeholder comment. The header carries the PR number and a title that says what the PR does for the reader, not the branch name; the meta line carries the repository, the branch, the state, the commit count, the file count and the test count.
3. What was achieved: two short paragraphs, the second on what it means for the way we work. Name the files only where the reader must go there.
4. How it works: one SVG diagram inside the template's viewBox, boxes and arrows with the template's classes (`box`, `new`, `bad`, `arrow`, `mono`, `cap`), at most eight boxes, text that fits its box at 8pt. One sentence of caption under it when the diagram needs one.
5. What to know, big picture: three bullets at most, each with a bold lead of a few words. A risk, a limitation, a decision owed, a number that changes what the reader does.
6. Progress: two meters. Left, the task (pieces done over pieces, percent with this PR counted). Right, the long term plan (decisions done over live decisions, with the arithmetic). The `width` of each meter span equals the percent.
7. House rules: no em dashes or double dashes, sentence case, no AI phrasing, no closing line that restates. The copy goes through `python <install folder>/tools/check_writing.py` only when it is kept as prose; the HTML is checked by reading the render.

## Render

1. `python <install folder>/tools/render_design.py ~/.claude/plans/pr-briefs/<name>.html --pdf --out ~/.claude/plans/pr-briefs` writes `<name>.pdf` and prints its absolute path. The install folder is the harness clone or bundle named in `~/.claude/local/machine.md`.
2. Read the PDF back with the Read tool. One page only; a second page means the copy is too long, cut it. Diagram text that is clipped by its box is fixed in the SVG, not by shrinking the font below 8pt.
3. Print the PDF path in the reply. When the ship or merge skill called this skill, that skill prints the path in its report.

## Done means

A single A4 page in `~/.claude/plans/pr-briefs/` whose four sections a reader can check against the PR, with every number carrying its arithmetic, rendered from the HTML next to it, and the path printed.
