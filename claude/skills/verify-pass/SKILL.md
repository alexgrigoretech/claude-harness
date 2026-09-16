---
name: verify-pass
description: Independent read-only verification of an implementation or a summary, claim by claim, with numbers measured next to numbers claimed. Use when the user says "verify", "verification pass", "check what codex did", "is this real", "audit the diff", or after any Codex run before the result is accepted.
---

# Verify pass

A summary from any agent is a claim, not evidence. This skill measures the claims.

1. Collect the claims: the Codex output file (CHANGED FILES, VERIFIED, ASSUMPTIONS/RISKS), the PR body, the handoff paragraph, or the user's description of what should be true. Number them.
2. Write the verification brief from `~/.claude/templates/codex-verification-brief.md` into the scratchpad: read-only rules, the throwaway worktree, the allowed probe commands, one section per claim group, and the standard checklist:
   1. every file under CHANGED FILES exists in the diff and the diff does what the line says;
   2. every VERIFIED command re-runs with the same result;
   3. every test the summary claims exists and fails when the behavior is broken (mutate one line, run, restore);
   4. nothing outside the allowed file list changed (`git diff --stat` against the base);
   5. the spec followed is the spec in the brief, not a plausible neighbour;
   6. no credential, client identifier or invented number entered the diff.
3. Run the verifier as a separate mind: Codex with `--sandbox read-only` on the brief, or a general-purpose subagent with the brief as its prompt, in a detached worktree at the ref under review. Never the session that wrote the code. The verifier saves every probe (command, output) as files in the brief's folder.
4. Read the verifier's report against the diff yourself; a verifier can also be wrong. Anything REFUTED or COULD NOT CHECK goes back to the implementer through `codex exec resume` with the measurement attached.
5. Report as a table: claim, evidence (command and observed value), verdict (CONFIRMED, REFUTED, COULD NOT CHECK), then the findings the verifier made outside the claims. Findings are worth more than confirmations. Do not summarise the claims back.
