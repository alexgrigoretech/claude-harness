---
# DESIGN.md for <name>. Tokens above the line are the only values UI code may use; prose below the line is the intent the tokens cannot carry.
# Replace every <placeholder>. Keep names, add or remove shades only with a note in the Decisions section. Lint: python <harness>/tools/check_design.py --design DESIGN.md <files>.
name: "<name>"
version: 1
base_unit: 4
color:
  primary:
    100: "<hex>"
    300: "<hex>"
    500: "<hex>"
    700: "<hex>"
    900: "<hex>"
  neutral:
    50: "<hex>"
    100: "<hex>"
    300: "<hex>"
    500: "<hex>"
    700: "<hex>"
    900: "<hex>"
  accent:
    500: "<hex>"
  semantic:
    background: neutral.50
    surface: neutral.100
    text: neutral.900
    text-muted: neutral.700
    border: neutral.300
    action: primary.500
    action-hover: primary.700
    action-text: neutral.50
    focus: accent.500
    success: "<hex>"
    warning: "<hex>"
    danger: "<hex>"
type:
  display:
    family: "<family, not Inter, Roboto, Arial or a system stack unless the Decisions section says why>"
    weights: [<weight>, <weight>]
  body:
    family: "<family, may equal display>"
    weights: [400, <weight>]
  scale:
    xs: 12
    sm: 14
    md: 16
    lg: 20
    xl: 24
    2xl: 30
    3xl: 36
    4xl: 48
  line_length_max_ch: 75
  line_height:
    body: 1.5
    heading: 1.15
spacing:
  scale: [4, 8, 12, 16, 24, 32, 48, 64, 96]
radius:
  sm: 4
  md: 8
  lg: 12
  full: 9999
shadow:
  sm: "<layered shadow, two layers, tinted toward the background hue>"
  md: "<layered shadow>"
motion:
  hover_ms: 120
  enter_ms: 240
  exit_ms: 160
  easing: "cubic-bezier(0.2, 0, 0, 1)"
  reduced_motion: required
breakpoints:
  phone: 390
  laptop: 1280
  wide: 1920
icons: "<one library, for example Lucide or Phosphor; never emoji as icons>"
---

# <name> design

## The one bold element

<One sentence naming the single element that carries the identity on every screen (a type treatment, a color block, a photographic style, a layout move). Everything else recedes. If two things compete, this section decides.>

## What this product must never look like

<Three to five concrete bans, specific to this product, beyond the generic tells (purple gradients, Inter everywhere, three identical cards, hover scale on cards, numbered 01 02 03 markers on content that is not a sequence). Example: no card grids for lists that are really tables; no gradients on text.>

## References

<Two or three real pages or design files looked at before this file was written, with one line each on what was taken from them and what was rejected. A reference is a URL or a file path, never an adjective.>

## Hierarchy

Three levels only, by size, weight and color together: primary text at `text` in the display or heading sizes, secondary at `text` in `md`, tertiary at `text-muted` in `sm`. Never grey text on a colored background; pick a shade of the background hue instead. Groups are separated by spacing from the scale, not by borders, and the space between groups is at least one step larger than the space within them.

## Layout

<Shell: sidebar or top bar, its width, the content max width, the grid (columns and gutter at laptop and wide), alignment rule (left aligned by default), how the phone layout differs (what collapses, what swaps from table to list).>

## States

Every list and form designs its empty, loading, error, dense and long-content states. Loading uses skeletons that mirror the final layout, shown after a 200 ms delay. Empty states say why they are empty and offer one action. Errors say what happened and what to do next, in the product's voice, without apology.

## Copy voice

<Tone in one line, the person addressed (second person), the verbs the product uses for its core actions, sentence case or title case for buttons, and the words that are banned.>

## Decisions

<Dated one-line entries: what changed in the tokens or the rules above and why. The newest entry first.>
