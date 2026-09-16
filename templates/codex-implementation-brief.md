# Brief <id>: <one-line task name>

Fill every section. A brief with a vague section produces a vague diff. Numbers in the brief are measured by the orchestrator and pasted; Codex never guesses a value, a column name or a path.

## Files you may touch

List them. Nothing else. Say explicitly what must not change (docs, macros, other modules, formatting of untouched lines). If a new file is allowed, name it. No new dependency unless it is named here.

## Behavior

State the exact behavior in the order it happens: inputs, transformation, outputs, error handling. Name the shared helper or module to reuse for anything that already exists (search first; the brief names the module). Include the edge cases with the measured counts behind them, the sentinel values, the encodings, the malformed inputs you have seen and what to do with each.

## Constraints

Style of the surrounding code, naming, the house rules that bite here (no em dashes, no markdown in descriptions, no truncation, plain prose in comments). Environment needed to run anything (paste the exact export lines). What is out of scope on purpose and why, so it is not "helpfully" done.

## Acceptance (run everything, paste results)

Concrete, checkable, in this form:
- `<command>` exits 0 and prints <what>.
- Test count goes from <n> to <n + k>.
- `git diff --check` clean; lint clean on every new file.
- List every file changed.
The orchestrator repeats these after the run; a claim without a pasted result counts as not done.

## Done means

One paragraph a stranger could verify: the observable state of the repo and its outputs when the task is complete. If part of the work cannot be verified by Codex (no warehouse access, no network), say so here and name who verifies it.

## Report

End with the required final message: CHANGED FILES, VERIFIED, ASSUMPTIONS/RISKS. The output file is a claim; the orchestrator reads the diff.
