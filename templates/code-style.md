---
paths:
  - "src/**"
  - "app/**"
  - "api/**"
  - "scripts/**"
  - "**/*.py"
  - "**/*.ts"
  - "**/*.tsx"
  - "**/*.sql"
---
# Code style and reuse (path-scoped rule, from the harness repo templates/code-style.md)

- Search before you write: an existing helper in the modules listed in `shared-modules.md` (or the nearest utils module) beats a new one. Name the module you reused in the brief and in the commit body.
- No new dependency without a line in the brief that allows it and says why. Codex refuses one on its own.
- No formatting sweeps. Do not reformat, reorder or rename lines you are not changing; a diff shows only the behavior change.
- Two copies of the same function are one too many: move it to the shared module once, then call it. Do not add a third.
- Comments and docstrings are plain prose read by tools: no markdown, no em dashes, identifiers as words.
- Errors are handled where they can be acted on, and named; no bare except, no swallowed failures, no printed traceback for an expected condition.
- After a non-trivial Codex round, run `/simplify` on the diff. Before push, `/code-review high`. Both before the PR, never after.
- Read `shared-modules.md` in this repo before touching anything under the paths above.
