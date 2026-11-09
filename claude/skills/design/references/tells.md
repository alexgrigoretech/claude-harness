# Tells: what generated interfaces look like right now

A tell is a default that appears whatever the subject. Each one is legitimate for some brief; the point is that it was not chosen for this one. The rubric counts tells by name; three or more fail a screen. When the brief or DESIGN.md asks for one of these on purpose, it is a decision, written in the Decisions section, and it does not count.

Sources: the five clusters named by Anthropic's own frontend-design skill (plugin 1.1.1), the Hallmark audit list, the Anti-AI-UI five axes, and what our own renders showed in November 2026. Review this file when the mean moves; a blocklist that is not maintained becomes a tell itself.

## The five clusters

1. Cream and terracotta: a warm cream background near `#F4F1EA`, a high contrast serif display, a terracotta or warm clay accent near `#D97757`. The accent is Anthropic's own interaction color, so on any other product it reads as borrowed.
2. Near black and acid: a near black background (`#0B0B0B`, `#111`, `#0a0a0a`) with a single bright acid green or vermilion accent, often with a soft glow behind the hero.
3. Broadsheet: hairline rules everywhere, zero border radius, dense newspaper columns, small caps running heads, a masthead that is not a masthead.
4. The SaaS card kit: content chopped into identical rounded cards, one border radius on everything regardless of hierarchy, the same soft grey shadow (`rgba(0,0,0,.1)`) under each, gradient washes as decoration, three cards in a row with an icon, a title and two lines each.
5. Template chrome: a tracked out ALL CAPS eyebrow label above every heading; meta strings joined with middle dots (A · B · C); labels built as WORD dash fragment with a spaced dash; tinted near black standing in for black; a monospace face for small data labels; an arrow glyph appended to link and button text.

## Color tells

- A violet to cyan (or violet to pink) gradient on near black, in the hero, on text, or as a glow behind a product shot.
- Gradient text: `background-clip: text` over any gradient.
- A different accent per section, so the page has four accents and no identity.
- Grey text on a colored surface instead of a shade of that surface's hue.
- Pure `#000` or `#fff` as a surface (not as a shadow or scrim), with no tint toward the brand hue.
- A glassmorphism panel (blur, white at 8 percent, a 1 pixel white border at 10 percent) over a gradient.

## Type tells

- Inter, Roboto, Arial, Open Sans, Lato or a system stack as the display face when nothing in the brief asked for it.
- One word of a headline in italic, bold or a different color.
- All caps for every label.
- A typographic label above content that did not need one ("FEATURES" above the features).
- Display and body in the same family at nearly the same size, so there is no display.
- Letter spacing opened on body text or closed on small text.

## Layout tells

- Numbered markers 01 02 03 on content that is not a sequence.
- Three identical cards, icon above title above two lines, in a row.
- A centered hero with a headline, a subline, two buttons (one filled, one outline) and a product screenshot below, repeated on every landing page.
- Everything centered.
- A bento grid of unrelated boxes.
- A logo wall of grey logos with an "as seen in" or "trusted by" line above it.
- A stat row of big numbers with small labels and a gradient accent, as the default hero treatment.
- A pricing table with three columns and the middle one raised and labelled popular.
- Full width sections that alternate background shade with no content reason.

## Motion tells

- Fade and slide up on every section as it enters the viewport.
- `transition: all` on everything.
- Hover scale on cards.
- A spinner where a skeleton that mirrors the layout belongs.
- Parallax on a product that is not about depth.
- Motion that ignores `prefers-reduced-motion`.

## Component tells

- Emoji as icons.
- Avatars with gradient initials as the only identity device.
- A toggle for something that is not binary, a dropdown for three options.
- Toasts for everything, including success on actions the user can see succeeded.
- Rounded pill buttons on a product whose radius scale says otherwise.
- The same shadow under every elevated thing.

## Copy tells

- Marketing verbs that promise without saying what happens: supercharge, effortless, the future of, and the rest of the AI phrasing list in `tools/check_writing.py`.
- "It's not X, it's Y".
- A triad in every headline.
- A closing line that restates the heading.
- A call to action that says Submit, Get started or Learn more where a verb from the product would say what happens.
- An error that apologises or is vague about what happened.
- An empty state with a cheerful line and no action.

## How to use this file

In shape mode, read a draft direction plan against every section and name the tells it leans on before the plan is shown; revise or write the decision. In review mode, count the tells visible in the render, by name, and apply the three tells rule from the rubric. In refine mode, removing a tell is always within budget; adding a device from this list never is.
