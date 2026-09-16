---
name: ship
description: Release ritual in a fixed order, gate then review then push then deploy. Use when the user says "ship", "commit push deploy", "push to prod", "push it", "commit and push", or asks to commit and release the current work.
---

# Ship

Run the release ritual for the current project. No questions unless a gate fails. Nothing is pushed unreviewed and nothing is deployed on a failed gate.

1. `git status` and `git diff --stat`. Nothing to commit and nothing undeployed: say so and stop. Confirm the branch and the remote; on a client repo, pushes and PR creation always prompt (permissions.ask), so expect the prompt.
2. Gate: run the project's build and test gate as the project CLAUDE.md defines it. Failure stops the ritual with the output pasted; do not deploy.
3. Review before push: run `/code-review high` on the diff. Docs-only diffs skip this step; say so. Every finding is fixed through `codex exec resume` on the session that produced the change (or a fresh delegate run if there was none), then the gate runs again. A review finding is never left for after the push.
4. Commit: stage by explicit path, never `git add -A`. Conventional subject, body wrapped at 72. No Co-Authored-By, Claude-Session or Generated-with lines; the commit-msg hook rejects them where installed.
5. Push to the current branch's remote. Open a PR when the branch is not the default branch and the project uses PRs; the PR body is written separately from the commit message and is not hard-wrapped. Merge only on the user's go.
6. Deploy per the project CLAUDE.md deploy section; if it defines a "needs VPS deploy" rule, apply it and say which path was taken and why. No deploy section: do steps 1 to 5 and ask which target applies, suggesting the CLAUDE.md line to add.
7. Post-deploy checklist if the project defines one (revalidation, sitemap or IndexNow ping, smoke check).
8. Report in one short block: commit hash, review result, gate result, what deployed where, checklist result.
