# Verification brief, pass <n>: <what is being verified>

You are an independent verifier. The implementation and its summary are claims; your job is to measure them, and to look where the implementer did not look.

## Context

Which commits or merges are under review (short shas), what previous passes verified and where their reports are, and what this pass covers that they did not.

## Rules

- Read-only. No git writes, no builds against a shared target, no writes to cloud resources. State the exact read-only commands that are allowed (queries through which profile, with which helper) and say what to do if credentials have expired: stop and report.
- Work in a throwaway worktree at the named ref; confirm `git log --oneline -1` matches before starting. Never touch the shared checkout.
- Every number in this brief is a claim to check, never a fact to reuse. Report the number you measured next to the number claimed. Where you cannot measure, say COULD NOT CHECK and why.
- Save every probe (query, execution id, rows) as a file named `verify<n>-<section>-<name>` in the brief's folder, and write the report there as well as to the last message.

## Sections, one per claim group

For each: the claims (with the numbers as stated), what to measure, and what to look at that the claim did not cover (what a new filter drops, what a rename leaves behind, whether docs and implementation agree, whether a fixture actually exercises the failing case).

Standard checklist for a Codex implementation pass, seeded from real failures:
1. Every file listed under CHANGED FILES exists in the diff and the diff does what the line says.
2. Every command listed under VERIFIED can be re-run and gives the same result.
3. Tests the summary claims were written exist and fail when the behavior is broken (mutate one line, run, restore).
4. Nothing outside the allowed file list changed (`git diff --stat` against the base).
5. The spec the implementer followed is the spec in the brief, not a plausible neighbour of it.
6. No credential, client identifier or invented number entered the diff.

## Report format

Per section: CONFIRMED, REFUTED, or COULD NOT CHECK; one line of what you ran; the numbers observed next to the numbers claimed; the finding in one or two sentences. Findings are worth more than confirmations. End with anything noticed outside the sections and the list of probe files written. Do not summarise the claims back. Remove the worktree only if you created it.
