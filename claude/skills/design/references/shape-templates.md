# Shape templates

Three templates the shape mode fills in order: the brief, the reference log, the direction plan (three copies). Keep each section to the lines shown; a brief longer than a screen is a sign that the product facts have not been found yet. Every fact comes from the project CLAUDE.md, the product docs, the existing code or the user; a line that cannot be sourced is marked `PROPOSED, confirm`.

## Brief

```
Screen or surface: <what is being designed, one line>
User: <who uses it, in the product's own words for its audience>
Job: <what they come here to get done, one sentence with a verb>
Success signal: <what we observe when it worked: an action completed, a number read, a decision taken>
Non-goals: <two or three things this screen does not do, so the design does not try>
Constraints: <framework, component library, the DESIGN.md tokens that are fixed, the widths that matter most>
Content: <real content or the closest real sample; never lorem ipsum>
```

## Reference log

Two or three entries, fetched before any direction is drawn. A reference is a URL or a file path. Public DESIGN.md files and design systems count; adjectives do not.

```
Reference 1: <URL or path>
  Taken: <one line: the device, palette move, type pairing or layout idea borrowed>
  Rejected: <one line: what this reference does that this product must not>
Reference 2: <URL or path>
  Taken:
  Rejected:
Reference 3 (optional): <URL or path>
  Taken:
  Rejected:
```

The chosen references go into the References section of DESIGN.md with the same two lines each.

## Direction plan

Three plans, named, each on this template. The plans differ in the bold element and the palette, not only in spacing.

```
Plan <letter>: <name, two or three words>

Palette (4 to 6 named values, each a DESIGN.md token or a proposed addition):
  <role>: <value> (<token name or "add">)
  ...

Type:
  display: <family>, <weights>, <where it appears>
  body: <family>, <weights>
  one line on why this pairing fits the user and the job

Layout at 1280 (ASCII wireframe, 12 to 20 lines):
  +------------------------------------------------------------+
  |                                                            |
  +------------------------------------------------------------+
  Alignment: <left by default; say where anything is centered and why>
  Phone (390): <what collapses, what swaps from table to list, what is dropped>

The one bold element: <one sentence; this is the only thing allowed to be loud>

Token deltas against DESIGN.md:
  add: <token: value>  (one per line)
  change: <token: old to new>
  remove: <token>
  (none is a valid answer and is written as "none")

Tells check: <the tells from tells.md this plan was close to, and what was changed to avoid them, or "none">

Generic test: <the result of running the brief without the product name; what differed, or what was revised because nothing did>
```

## Recommendation

After the three plans, one paragraph: which plan is recommended, the one tradeoff that decides it, and the question the user must answer before build starts. Then stop.
