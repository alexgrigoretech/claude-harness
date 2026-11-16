---
name: ship
description: Release ritual in a fixed order, gate then review then push then deploy. Use when the user says "ship", "commit push deploy", "push to prod", "push it", "commit and push", or asks to commit and release the current work.
---

# Ship

Run the release ritual for the current project. No questions unless a gate fails. Nothing is pushed unreviewed and nothing is deployed on a failed gate.

1. `git status` and `git diff --stat`. Nothing to commit and nothing undeployed: say so and stop. Confirm the branch and the remote; on a client repo, pushes and PR creation always prompt (permissions.ask), so expect the prompt.
2. Gate: run the project's build and test gate as the project CLAUDE.md defines it. When the repository has a `DESIGN.md` and the diff touches the project's own UI files (css, scss, html, jsx, tsx, vue, svelte; not templates or skill files shipped under `claude/` or `templates/`), also run `python <harness install folder>/tools/check_design.py --design DESIGN.md <changed UI files>` (the install folder is named in `~/.claude/local/machine.md`); any nonzero exit is a gate failure like any other. Failure stops the ritual with the output pasted; do not deploy.
3. Review before push: run `/code-review medium` on the diff. Docs-only diffs skip this step; say so. Every finding is fixed through the delegate skill's resume step on the implementer session that produced the change (or a fresh delegate run if there was none), then the gate runs again. A review finding is never left for after the push.
4. Commit: one commit for the branch's work of the day (the commit rule in the global CLAUDE.md Git section). When the branch already has a commit from today that is not pushed, amend it; when today's commit is pushed, add one follow-up commit that carries every fix since, review fixes included, never one commit per finding or per round. Stage by explicit path, never `git add -A`. Conventional subject, body wrapped at 72. No Co-Authored-By, Claude-Session or Generated-with lines; the commit-msg hook rejects them where installed.
5. Scope check, before the push when a PR will be opened or updated: write the one problem the PR solves in one sentence and read `git diff --stat <base>...HEAD` against it (the one problem rule in the global CLAUDE.md Git section). A file the sentence does not need is a stop: move it to its own branch when that is mechanical (a separate file, no shared hunk), otherwise list it and ask. Review findings outside the problem go to the follow-up list in the handoff, not into this branch. The sentence opens the PR body. Then push to the current branch's remote. Open a PR when the branch is not the default branch and the project uses PRs; the PR body is written separately from the commit message, is not hard-wrapped, and is short and on point with no dates, counts or other proof (the PR body rule in the global CLAUDE.md writing rules); where Muse Code is installed and the repo is not a client repo, the draft skill writes it (Muse Wonder Full, then the humanizer gate). Once the PR exists, run the pr-brief skill (the one page A4 PDF in `~/.claude/plans/pr-briefs/`, local only) and print its path. Merge only on the user's go.
6. Deploy per the project CLAUDE.md deploy section; if it defines a "needs VPS deploy" rule, apply it and say which path was taken and why. No deploy section: do steps 1 to 5 and ask which target applies, suggesting the CLAUDE.md line to add.
7. Post-deploy checklist if the project defines one (revalidation, sitemap or IndexNow ping, smoke check).
8. Report in one short block: commit hash, review result, gate result, the PR brief path when a PR was opened, what deployed where, checklist result.

## Not done when

A state on this list looks finished and is not. Report it as the state it is, never as done; the status skill reads this list too.

- The gate ran on a tree other than the one committed, because a fix landed after the gate.
- The branch is pushed and the PR is open, but its checks are pending or failed. Ship does not wait for them: report "pushed, checks pending" (or failed, with the failing names), and the merge skill's step 4 waits.
- The PR exists and its brief PDF does not.
- The deploy command returned, but the post-deploy checklist or smoke check did not run.
