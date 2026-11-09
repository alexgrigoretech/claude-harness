# Design review rubric

Eight dimensions, each scored 1 to 5 against the written anchors below, from a render (a PNG read by the reviewer), never from the code. Each score carries one sentence of reason that names what in the render produced it. Findings are listed separately with a severity, a location and the smallest fix.

## Hard rule

Three or more tells from `tells.md` fail the screen regardless of score. Record the tells by name, then the verdict. A screen passes when all three hold: fewer than three tells, no P0 finding, and no dimension below 3. Any one missing is a fail, whatever the total.

## Scores

A 5 is rare and must name the evidence. A 3 means the screen does the job without distinction. A 1 means the dimension damages the screen. Score half points never; round down.

### 1. Hierarchy and type

- 5: three levels at most, each separated by size, weight and color together; the eye lands on the primary element first at every width; the display family does visible work.
- 4: three levels, one of them separated by a single attribute only.
- 3: hierarchy readable but four or more levels, or two levels that compete for the first glance.
- 2: body and heading sizes too close, or weight used as the only separator, or headings in the body family with no other difference.
- 1: no visible hierarchy; the screen reads as one block.

### 2. Spacing and alignment

- 5: every gap is a step of the DESIGN.md scale; space between groups is at least one step larger than space within; one alignment axis per region.
- 4: scale respected, one group where inner and outer spacing are equal.
- 3: a few off scale values with no visible damage.
- 2: inconsistent gutters between siblings, or mixed left and center alignment in one region.
- 1: elements touch or overlap, or alignment changes with no rule.

### 3. Color

- 5: tokens only; the accent appears on the primary action and the focus ring and nowhere else; neutrals are tinted toward the background hue.
- 4: tokens only, accent used in one extra place that can be defended.
- 3: one or two raw colors that the checker caught; accent spread over three places.
- 2: palette changes between sections, or grey text on a colored background.
- 1: colors unrelated to DESIGN.md, gradients as decoration, or the whole screen in the accent.

### 4. Contrast and readability

- 5: body text at 4.5:1 or better and large text at 3:1 against its surface on both the light and the dark render; line length under the DESIGN.md maximum; line height from the file.
- 4: one secondary text below the ratio.
- 3: muted text borderline at phone width, or line length over the maximum on wide screens.
- 2: primary text below 4.5:1 anywhere, or the dark render loses a region.
- 1: unreadable text in either render.

### 5. Composition and the one bold element

- 5: the DESIGN.md bold element is present and is the only bold thing; everything around it recedes; the phone layout keeps it.
- 4: the bold element is present but shares attention with one other element.
- 3: the bold element is present but weak, or two elements of equal weight.
- 2: no bold element; the screen is even and forgettable.
- 1: several loud elements compete, or the bold element contradicts DESIGN.md.

### 6. States

- 5: empty, loading, error, dense and long content states are designed and rendered; loading mirrors the final layout; empty states say why and offer one action.
- 4: all states designed, one not rendered.
- 3: happy path plus empty and error; loading and dense missing.
- 2: happy path only, states inferred from the code.
- 1: a state breaks the layout (overflow, clipped text, a spinner with no layout).

### 7. Consistency with DESIGN.md

- 5: checker exit 0; type, radius, shadow and motion values match the file; the Decisions section explains every deviation.
- 4: checker exit 0; one undocumented deviation in radius, shadow or motion.
- 3: checker findings in one rule only; the file needed a token that was then added with a Decisions line.
- 2: checker findings in three or more rules; the screen reads as a different product from the rest.
- 1: DESIGN.md ignored or absent and not created.

### 8. Originality against the tells

- 5: zero tells; the direction would not be mistaken for another product's; the references in DESIGN.md are visible in what was taken and in what was rejected.
- 4: one tell, and it is a defended choice written in DESIGN.md.
- 3: one or two tells not written down.
- 2: two tells plus the generic template chrome.
- 1: three or more tells (also fails under the hard rule).

## Findings

Each finding has a severity, a location (file and line, or the region of the render and the width), one sentence on what is wrong, and the smallest fix that resolves it.

- P0: blocks release. Unreadable text, overflow or clipping at any width, a state that breaks the layout, keyboard focus invisible, motion that ignores reduced motion, contrast below 3:1 on primary text.
- P1: must fix before the user is shown the screen. Raw colors on visible elements, the bold element missing, the hierarchy inverted, a tell on the hero.
- P2: fix in this round if the budget allows. Off scale spacing, one accent too many, a dark render that is merely dull, an undocumented radius.
- P3: note for a later round. Copy polish, a minor alignment wobble, a reference worth adding to DESIGN.md.

## Verdict

One line: pass or fail by the rule above, the tells count, the lowest dimension and its score, the number of P0 and P1 findings. A fail names the single change that would move the verdict.
