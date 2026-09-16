# Humanizer changelog (house fork)

## 3.1.1-house (2026-09-14)

First fresh-session run of both eval cases (headless `claude -p`, tools Read and Skill). jira-comment passed (15 of 15 tells, no false flags). linkedin-post caught 16 of 17 and flagged one acceptable colon. Two wording fixes, no new patterns: H1 now says a colon that delivers the answer the sentence set up is fine, the tell is a clause on each side; 19 now names the label that restates its own line as a separate tell. Re-run of linkedin-post after the fix: 17 of 17, no false flags, figures intact. Both cases pass, hence the bump.

## 3.1.0-house (2026-09-13)

Rebased onto upstream blader/humanizer 3.0.0 (SKILL.md on main, fetched 2026-09-13). Replaces 2.6.0 everywhere the harness is installed. Version bump rule from now on: no bump without both cases under evals/ passing in a fresh session.

### Upstream diff, 2.x base of 2.6.0 to 3.0.0

- 29 patterns in six flat sections became 25 patterns in five sections (A staging, B rhythm, C inflation, D formatting, E leftovers), ordered strongest first, with *weak alone* marks on patterns that need company.
- New: "Why AI text sounds the way it does" (the default-choice explanation), a four-step "How to work" with an explicit check for added or dropped facts, file mode and embedded mode outputs, and a "When not to act" section (quotes, titles, pre-2022 text, voice details to keep).
- New patterns: one-line closers and dramatic fragments (2), sayings that sound deep (3, absorbs the old persuasive authority tropes), arguing with no one (5), repeated sentence openings (7), vague connection or association (14), writing about the previous version (25).
- Merged: significance, challenges sections and generic conclusions into one pattern (13); notability lists and vague attributions into borrowed authority (17); bold overuse and inline-header lists into bold as decoration (19); title case and emojis into decorative headings (20); signposting into staged run-up (4).
- Dropped upstream: the separate "Personality and soul" section and the voice calibration walkthrough (folded into a short "Voice" note), the numbered output format with the two audit prompts (now step 3 of "How to work"), the long worked example. The soul list survives in this fork under Quick pass.
- Dashes (8): upstream allows a dash when the writer's sample uses them. The house override removes that allowance.

### House additions, with origin

| Item | Origin |
|---|---|
| H1 colons as mid-sentence connectors | pstack unslop rule 14, ported 2026-08-19 as humanizer 2.6.0 pattern 30 |
| H2 abstract metaphor nouns | pstack unslop rule 26, was pattern 31 |
| H3 naming a feeling instead of the mechanism | pstack unslop rule 27, was pattern 32 |
| H4 adverbs propping up weak verbs | pstack unslop rule 30, was pattern 33 |
| H5 prefer the plain word | pstack unslop rule 31 plus the 2.x filler list, was pattern 34 |
| Override: em dashes banned everywhere, sample or not | global CLAUDE.md writing rule |
| Override: parentheses allowed | global CLAUDE.md; reverses pstack's ban of parentheses as a dash substitute |
| Override: Romanian in ASCII | global deliverable rule, stated 2026-07-14 |
| Override: humanizer is the mandatory final gate on prose | global CLAUDE.md |
| Quick pass section (34 numbered rules plus "Adding soul") | the standalone unslop skill 1.0.1 (pstack port), folded in; rules 32 to 34 added from upstream 3.0.0 patterns 2, 4 and 5 |
| Description triggers: humanize, unslop, run the humanizer, make it sound human, does this sound AI | harness plan WS-D |
| Section 8 example written with [em dash] placeholders | keeps the skill file itself free of the character, so the repo checker can grep for it |
| evals/ with two cases (LinkedIn post, Jira comment) | harness plan WS-D, D16 |

### Removed

- The standalone `unslop` skill (zero launches on any machine per the 2026-09-13 audit). Its content lives in the Quick pass section. Deletion of the old folder is handled by the L2 install manifest.
- The 2.6.0 "Full example" walkthrough.

## 2.6.0 (2026-08-19)

Added patterns 30 to 34 from pstack's unslop skill to the 2.5.1 base (blader/humanizer 2.x). Parentheses kept allowed against pstack's ban. Shared with a colleague through the installer doc on 2026-08-20 (1.0.1 unslop edit dropped a job-seeking mention).

## 2.5.1 and earlier

Upstream blader/humanizer, patterns 1 to 29 from Wikipedia's "Signs of AI writing".
