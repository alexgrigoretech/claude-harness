# Project rules for Codex (source: the harness repository, templates/AGENTS.md; do not edit here)

The machine-wide house rules live in `~/.codex/AGENTS.md`. This file adds only what is specific to a project laid down by the harness.

Before writing code, read `.claude/rules/code-style.md` and `shared-modules.md` at the repository root. Name the shared module the change reuses or say that none applies, and follow the code-style rule as written: two copies of a function are one too many, so do not add a third copy.

The brief names the files to touch and the acceptance commands. Run those commands and paste the results under VERIFIED. Do not touch a file outside the brief's list.

`CLAUDE.md` at the root carries the build and test gate and the handoff conventions. Read it once at the start of a task.
