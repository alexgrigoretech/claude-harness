# Expected findings, eval case 2 (Jira comment)

A passing run lists every tell below, keeps every number and state, returns one paragraph per line (embedded mode, Jira rendering), and flags nothing from the "acceptable" list.

## Tells that must be caught

| Section | Tell in the input |
|---|---|
| 22 chatbot residue | "Great question!", "Let me know if you have any questions. Hope this helps!" |
| 18 emojis on a heading line | rocket on the first line |
| 5 arguing with no one | "To be clear, I'm not saying the outbox was wrong." |
| 4 staged candor | "Honestly? The root cause was elsewhere." |
| 19 bold labels on every item | Status:, Root Cause:, Fix:, Verification: |
| 24 heading repeated in the first sentence | "Status: The status is that the fix is in." |
| 8 dashes, House override | the double hyphen used as a dash in the Root Cause line (written as two hyphens so the eval file itself stays free of the em dash character) |
| 12 AI words, 13 inflated significance | "subtle but crucial issue that underscored the importance", "robust", "represents a significant step forward" |
| 25 writing about the previous version | "The previous approach of validating after publish has been removed." |
| 15 shallow -ing rider | "highlighting the robustness of the new flow" |
| 1 not X but Y | "It's not just a bugfix, it's a hardening of the whole pipeline." |
| 10 hyphenated pairs stacked | "cross-functional, well-known, end-to-end sign-off" (also a triad, section 6) |
| 11 passive voice | "was being skipped", "is now enforced", "were replayed" (fix the ones where the actor matters: the connector skipped validation, the publisher validates before publish) |
| 23 knowledge-limit guess | "While specific details about the ACME-side retry policy are not fully documented, it likely retries three times." (state that it is undocumented or cut; never keep the guess as a fact) |
| H2 abstract metaphor | "Release 1.2.15 is the vehicle for this change" |

## Must survive unchanged

TICKET-42, 1.2.15, 3 of 3 integration tests, 412 records, 0 failures, Wednesday, dev environment, the causal chain (payload re-serialized by the ERP connector, validation skipped, validation now runs before publish).

## Acceptable, must not be flagged

- Placeholders ACME, the ERP, the event bus, TICKET-42 (they are the anonymization form, not a tell).
- A parenthetical aside if the rewrite adds one.
- A colon before the list of what was verified.
- A greeting-free comment with no sign-off: Jira comments do not need one.

## Reference rewrite (embedded mode, one acceptable outcome)

Fix for TICKET-42 is in.
Root cause: the ERP connector re-serialized some payloads, and the validation step skipped those messages.
Fix: the publisher now validates every record before publish.
Verified: 3 of 3 integration tests pass; 412 records replayed against the dev event bus with 0 failures.
Ships in release 1.2.15. Deployment to dev is planned for Wednesday after sign-off.
The ACME-side retry policy is not documented on our side; I have asked for it.
