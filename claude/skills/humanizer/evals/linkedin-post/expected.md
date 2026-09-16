# Expected findings, eval case 1 (LinkedIn post on GRR)

A passing run lists every tell below (by number or by name), keeps every figure, and flags nothing from the "acceptable" list.

## Tells that must be caught

| Section | Tell in the input |
|---|---|
| 4 staged run-up | "Let's dive into a metric that every SaaS founder should know" |
| 20 decorative headings, 18 emojis | rocket emoji on the opener; emoji on every bullet |
| 2 one-line closer | "Read that again." and "That is the real win." |
| 1 not X but Y | "It's not just about the number you report; it's about the window you choose." |
| 13 inflated significance, 18 copula avoidance | "stands as a testament to rigor and serves as the gold standard" |
| 19 bold labels on a list | the four bold-label bullets (Formula:, Downgrades:, Failed Payments:, Netting:) |
| 19 label restates the line | "Downgrades: Downgrades count", "Failed Payments: Failed payments count" |
| 12 AI words | "robust", "meticulous", "leverage" |
| 15 shallow -ing riders | "ensuring a robust and meticulous calculation", "highlighting the importance of involuntary churn" |
| 3 sayings that sound deep | "At its core, what really matters is discipline." |
| 17 borrowed authority | "Industry experts agree that..." (the source is Benchmarkit, keep the citation, drop the experts) |
| 8 dashes, House override | the double hyphen in "retention -- a truly staggering number" |
| H4 adverb on a weak verb | "a truly staggering number" |
| 6 forced triad | "rigorous, transparent, and data-driven" |
| 10 hyphenated pair | "data-driven" |
| 13 send-off | "In conclusion, the future looks bright for founders..." |
| 22 chatbot residue | "Let me know if you'd like the full guide!" |
| 11 passive | "Expansion is excluded", "the number is capped" (weak alone, acceptable to leave if the rest is fixed) |

## Must survive unchanged

99%, 88.6%, 84%, 88%, 110, KeyBanc 2019, Benchmarkit 2026, the formula itself, the four rules (downgrades count, failed payments count, net within an account, expansion excluded), the closing instruction to write the definition down.

## Acceptable, must not be flagged

- The parenthetical "(never across accounts)" and "(Benchmarkit, 2026)". Parentheses are allowed by the House override.
- "NRR wearing a GRR name tag": a specific, voiced image, not an aphorism.
- The colon in "one direction: up" introduces the answer, not a welded clause.
- Straight quotes and the percent signs.

## Reference rewrite (one acceptable outcome, not the only one)

99% monthly retention compounds to 88.6% annual. Both describe the same company. Only the window changed.

Here's the GRR version that survives a diligence read:

(Starting ARR - churn - downgrades) / starting ARR. Expansion excluded, so the number caps at 100%.

Downgrades count. Counting only full cancellations is the classic inflation trick in private SaaS.

Failed payments count. Involuntary churn is still churn.

Net within an account, never across accounts. Cross-account netting is NRR wearing a GRR name tag.

The bar: 84% median GRR for private B2B SaaS, down from 88% a year earlier (Benchmarkit, 2026).

A 2019 KeyBanc study counted 110 different ways companies calculate retention. Write yours down in one place. Retention definitions only ever drift in one direction: up.
