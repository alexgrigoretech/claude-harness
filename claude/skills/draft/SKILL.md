---
name: draft
description: Draft prose with Muse Wonder Full through the Muse Code CLI, then pass it through the humanizer gate. Use when the user says "draft", "draft with muse", "muse this", "write the PR body", "jira description", "jira comment", "write the ticket", "write the doc", or asks for a first draft of any text a human will read.
---

# Draft

Muse Wonder Full writes the first draft; Claude briefs it, gates the result and stays the final editor. Nothing this skill produces is posted anywhere without the user's go.

1. Data gate first. Muse Code sends the prompt to Meta. When `~/.claude/local/machine.json` has `"client": true`, or the material comes from the client-notes folder named in `~/.claude/local/machine.local.md`, a client repository or a client ticket, do not call Muse: draft with the session model and say "drafted locally (client material)". The wrapper already passes `--contributor-mode off` and `--no-foreign-personal-context`; never call the muse binary directly from this skill.
2. Collect the material: the diff or commit list for a PR body, the ticket, thread or notes for a Jira description or comment, the outline or source notes for a doc. Measured facts only; a number or name that is not in the material does not go into the prompt.
3. Write the prompt to the session scratchpad as `draft-<topic>-prompt.md`: the kind of text and where it will be read, the audience, the material, and the house rules that bite for that surface. PR bodies and GitHub comments: markdown, no hard wrap, no attribution lines. Jira descriptions and comments: Jira wiki markup or plain text as the project uses, no markdown, no hard wrap, one paragraph per line. Docs: the project's format. Everything: no em dashes or double dashes, no AI-tell phrasing, no closing paragraph that restates, Romanian without diacritics when the user writes in Romanian. Tell Muse to return the text only, no preamble and no explanation.
4. Run from the Bash tool, with the harness install folder from `~/.claude/local/machine.md`: `python <install folder>/tools/muse_llm.py --raw --effort high < <scratch>/draft-<topic>-prompt.md > <scratch>/draft-<topic>.md`. Exit 3 means Muse Code is not installed on this machine: draft with the session model and say so. Any other nonzero exit: paste the stderr line and draft locally.
5. Gate: run the humanizer skill on the draft in file mode, then `python <install folder>/checkers/check_writing.py <scratch>/draft-<topic>.md`. Fix every finding before showing the text.
6. Show the final text in one block and name the source ("Muse Wonder Full, humanized" or "drafted locally"). Posting to Jira, GitHub or any shared channel waits for the user's explicit go, then goes through the project's own tooling.
