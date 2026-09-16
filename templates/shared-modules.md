# Shared modules (template, one copy per repo at the repo root)

The reuse rule in `.claude/rules/code-style.md` points here. One row per module that more than one part of the codebase should call instead of re-implementing. Keep it short; a row nobody would look up does not belong.

| Module | What it owns | Use it for | Do not use it for |
|---|---|---|---|
| `<path/to/module>` | <the one responsibility> | <the calls other code should make> | <the tempting misuse> |

Rules for this file:
- Add a row when a second caller appears, not before.
- The brief for any task under the rule's paths names the row it reuses or says "no shared module applies".
- A row whose module was removed is deleted the same day.
