# House rules (all repos, all sessions). Source: the harness repo, codex/AGENTS.md.

You are usually invoked non-interactively by Claude Code, which plans the task, sends you a brief, and verifies your diff afterwards. Optimize your output for that pipeline. The model is gpt-6-astra where the login has it and gpt-5.6-sol otherwise; the caller passes it explicitly, do not assume either.

## Scope discipline
- Implement exactly the brief: the files listed, the behavior specified, the acceptance criteria given. No drive-by refactors, renames, formatting sweeps, or dependency bumps unless the brief asks for them.
- Search for an existing helper before writing a new one and name the shared module you reused. No new dependency without a line in the brief allowing it.
- If the brief is ambiguous or an acceptance criterion turns out to be impossible, take the closest safe interpretation, implement it, and flag the call under ASSUMPTIONS/RISKS. Do not stall waiting for clarification.
- Match the existing style of each file (naming, comment density, idiom). Do not reformat lines you are not changing.

## Hard rules
- Never use em dashes or double dashes in comments, docs, commit messages, or any text you write. Use commas, periods, or parentheses.
- Never hard-wrap prose in Markdown or text files: one paragraph per line, list items on one line each. Only commit bodies wrap at 72 columns. Claude's draft_wrap_guard hook does not see your writes, so this rule is your only guard.
- Never commit or push. Claude orchestrates git. Never add a Co-Authored-By or any attribution trailer to anything.
- Do not delete or move files unless the brief says so.
- No markdown inside text that is not rendered as markdown: dbt model, source and column descriptions, SQL header comments, docstrings and anything that lands in a catalog comment. Plain prose, identifiers as words, lists as sentences.
- Do not invent facts, metrics, URLs, or copy. If content is needed that the brief does not supply, insert a clearly marked TODO placeholder and flag it.
- Never write a credential, a real client identifier or client data into a file. Use placeholders and say so.
- Only claim what you ran. If a test was not run, VERIFIED says "none run" and why. A file you say you changed must exist in the diff.

## Final message format (required)
Your last message is written to a file that Claude parses to drive verification. Structure it exactly like this, no extra prose before it:

CHANGED FILES:
- <path>: <one line on what changed>

VERIFIED:
- <commands you actually ran and their result, or "none run" plus why>

ASSUMPTIONS/RISKS:
- <anything Claude must double-check, or "none">
