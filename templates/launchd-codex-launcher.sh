#!/bin/bash
# launchd launcher contract for a detached Codex run on macOS.
# 1. launchd starts with an empty environment: export HOME and PATH explicitly.
# 2. Absolute paths everywhere; no ~ and no relative paths.
# 3. Append a start line and an exit line to the log so the run is reconstructable.
# 4. The output directory for --output-last-message must exist before the run.
# 5. Remove the launchd job at the end so it never fires twice.
export HOME=/Users/<user>
export PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
B=/Users/<user>/dev/<repo>/.scratch/<date>-<topic>
mkdir -p "$B"
cd /Users/<user>/dev/<repo>
git fetch origin -q
git worktree remove --force /Users/<user>/dev/<repo>-verify 2>/dev/null
git worktree add -q --detach /Users/<user>/dev/<repo>-verify origin/main
cd /Users/<user>/dev/<repo>-verify
echo "start $(date -u +%H:%M:%SZ) at $(git log --oneline -1)" >> "$B/codex.log"
codex exec -C "$PWD" --sandbox workspace-write -m gpt-6-astra -c model_reasoning_effort="high" --output-last-message "$B/codex-out.md" - < "$B/brief.md" >> "$B/codex.log" 2>&1
echo "exit=$?" >> "$B/codex.log"
launchctl remove codex.<topic>
