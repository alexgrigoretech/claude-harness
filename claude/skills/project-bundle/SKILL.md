---
name: project-bundle
description: 'Build and restore a gated zip of untracked project context. Use when the user says "project bundle", "bundle the project context", "pack the handoffs", or "unpack the project bundle".'
model: sonnet
---

# Project bundle

1. Locate the harness install folder and the interpreter named in `~/.claude/local/machine.md`. Run the tool from that folder, which must contain `install.py`. The gate uses the term lists present on the machine; a machine with none refuses unless the allow-ungated flag is passed, and an ungated zip is read once by a person before it is shared. Use the current repository unless the user supplies another path. Name the zip `project-bundle-<repo basename>-<date>.zip`, using the UTC date.
2. Run the interpreter with `tools/project_bundle.py build <repo>` and its output option pointing to the zip. Enable the dry run option first. Read the kept paths, skipped lines and gate record. Add the worktrees option when linked worktree context is requested, and repeat the include option for any requested extra glob. Pass `--exclude <glob>` for any kept file that is not a note, including generated pages, deliverables that belong in a commit and personal records, and repeat the dry run until the kept list holds notes only. In all globs, `*` matches within one path segment and `**` spans directories: `--exclude 'docs/handoff-*timesheet*.md'` matches directly under docs, while `--exclude 'docs/**/handoff-*timesheet*.md'` matches at any depth under docs. HTML is omitted by default and requires an explicit include glob. The home option selects the home containing machine.json ownership and the domain lists.
3. Unless the user requested only a preview, repeat the build without the dry run option. A gate refusal must be resolved before a zip can be shared.
4. Independently unzip the result into the scratch folder and search the extracted tree for the names of the other engagements before handing the zip over. The gate covers terms, not identifiers inside the notes, so the person reads the manifest table once. Report the zip path and any unresolved findings.
5. On receipt, run `tools/project_bundle.py list <zip>`, then `tools/project_bundle.py apply <zip> <repo>` with the dry run option, then repeat apply without that option. Existing files are always skipped. Worktree context lands under `docs/from-worktrees/<name>/` for the receiver to place as needed.
