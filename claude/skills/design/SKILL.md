---
name: design
description: Shape, build, review, refine or write copy for a user interface against the project's DESIGN.md, with a rendered screenshot loop and a scored rubric. Use when the user says "design", "design this", "UI", "UX", "mockup", "landing page", "dashboard layout", "redesign", "design review", "make it look better", "does this look generic", or asks for any screen, page or component to be drawn, judged or improved. Not for CSS bug fixes with no visual decision in them, which are ordinary code work.
---

# Design

Every visual decision in a project comes from its DESIGN.md, every visual claim comes from a render, and every review is a score against the rubric in `references/rubric.md`. The skill never says a screen looks right from the code.

## Model rule

Design content, CSS and HTML are written by Fable or Opus: the session model when it is one of those, otherwise an Opus agent through the Agent tool. Never Codex, Sonnet or Haiku. Codex implements code only after a rendered design has been approved, from a brief through the delegate skill that names the chosen direction plan and the DESIGN.md tokens. A bounded refine round under the iteration gate may run on a cheaper tier, as `references/iteration-gate.md` describes.

## Modes

Pick the narrowest verb that fits the request and say which mode runs. More than one may run in sequence (shape then build then review is the normal path for new work).

- shape: there is no agreed direction yet. Produces the brief and three direction plans, picks one with the user.
- build: a direction plan exists. Produces the markup and styles and the renders.
- review: a screen exists, rendered or live. Produces the scored rubric and the findings list, changes nothing.
- refine: a review produced findings. Applies the smallest fixes under the iteration gate, re-renders, re-scores.
- copy: the words on a screen. Produces the text in the DESIGN.md voice, never layout or color changes.

## Source of truth

1. Read `DESIGN.md` at the repository root first. The YAML front matter holds the tokens, the prose below holds the intent (the one bold element, what the product must never look like, references, hierarchy, layout, states, copy voice, decisions).
2. No DESIGN.md: create one from `<install folder>/templates/DESIGN.md` (the install folder is named in `~/.claude/local/machine.md`), with the project name in the front matter and every placeholder either filled from the existing code and mockups or marked `PROPOSED, confirm` in the Decisions section. Say that the file was created. Do not run project-init for this; it lays down more than the design file.
3. Tokens only. UI code uses the DESIGN.md values and nothing else. A value the design needs and the file lacks is added to the file first, with a dated line in Decisions, then used.

## Shape

1. Brief first, in the shape of `references/shape-templates.md`: who the user is, the job this screen does for them, the signal that tells us it worked, and the non-goals. Take these from the project CLAUDE.md, the product docs and the user; never invent a fact about the product or its audience.
2. Fetch and log two or three real references before proposing anything: public pages, public DESIGN.md files or design systems, each as a URL or a file path with one line on what is taken and what is rejected. A reference is never an adjective. Log them in the reference log of the templates file; the chosen ones go into the References section of DESIGN.md.
3. Three direction plans, each with: a named palette of four to six values, the type roles (display and body families with weights), an ASCII wireframe at laptop width with a note on what the phone layout drops, the one bold element, and the token deltas against DESIGN.md (added, changed, removed, each one line).
4. Reject any plan that is what you would produce for a generic brief: work through the same brief with the product name removed and compare. A plan that survives that comparison unchanged is generic; revise it and say what changed and why. Check every plan against `references/tells.md`; a plan that leans on a listed tell is revised before it is shown.
5. Present the three plans, recommend one, and stop for the user's choice. Shape never writes code.

## Build

1. Follow the chosen plan and the DESIGN.md tokens. Write the markup and the styles yourself (the model rule); hand Codex only the non-visual code, after the render is approved.
2. The render loop is mandatory. After every build step run `python <install folder>/tools/render_design.py <file or URL> --out .scratch/render` at the default widths 390, 1280 and 1920, then once more with `--dark` and once with `--reduced-motion`. Read the PNGs with the Read tool before saying anything about how the screen looks. A claim of visual verification that did not come from reading a render is a false claim.
3. Run `python <install folder>/tools/check_design.py --design DESIGN.md <changed files>` on every changed UI file. Exit 1 is a stop: fix the finding or add the token to DESIGN.md with a Decisions line, then run again.
4. Score the render against `references/rubric.md` before reporting. Three or more tells from `references/tells.md` fail the screen whatever the score.
5. Report with the render paths, the checker output and the rubric scores. Clipped text, overflow at 390 or an unreadable dark variant are findings, not footnotes.

## Review

1. Render the target the same way as in build (three widths, dark, reduced motion) unless PNGs from the current state already exist; read them.
2. Run the checker over the UI sources and keep the per rule counts.
3. Score the eight dimensions 1 to 5 with the written anchors and one sentence of reason each, list the findings as P0 to P3 with location and the smallest fix, count the tells, and apply the three tells rule.
4. Write the result to a findings file the user names (default `.scratch/design-findings.md`). Review changes no code; the user decides whether a refine round follows.

## Refine

Runs only after a review, under `references/iteration-gate.md`: subtract or refine, never add scope; stay inside the per round budgets; a check that passed once and fails later is a regression that halts the round and rolls back to the last passing state; re-render and re-score after each round. New scope needs a new brief through shape.

## Copy

Use the Copy voice section of DESIGN.md: the person addressed, the verbs for the core actions, the case for buttons, the banned words. Active voice, the action keeps its name through the whole flow, errors say what happened and what to do next without apology, empty states say why and offer one action. Run `python <install folder>/tools/check_writing.py` on any copy file the project keeps as prose. Copy never changes layout or color.

## Report

One short block: mode run, DESIGN.md decisions added, render paths read, checker result, rubric scores with the tells count, what the user decides next. When Codex implements after approval, the delegate skill's report follows.
