---
name: status
description: Answer questions about a running or finished job from logs and output files without starting any work. Use when the user says "status", "eta", "is it done", "so it's done?", "everything done?", "CI green yet?", "when should I be back", or asks how a running job is doing. Plan-level questions (where the whole plan stands, what changed in the plan) belong to the masterplan and progress skills where the plan tools are installed; otherwise answer them from the newest handoff file.
model: haiku
---

# Status

Answer the progress question from evidence on disk and running processes. Never start, resume or change work; if the answer is "not started", say so.

Look, in this order, and stop as soon as the answer is clear:
1. Background task output files and Codex output files named in the conversation or in the newest `HANDOFF-*.md` in the working directory (`codex-*-out.md`, `*.log`, `codex-*-run.log`). An output file that does not exist yet means the run has not finished; a run log ending in `exit=0` with no output file means a quota exhaustion, not success.
2. Running agent processes: `tasklist` on Windows, `ps -eo pid,etime,args` elsewhere, filtered to `codex` and `claude`. A process with a long elapsed time and no new log lines is stalled.
3. `git status --porcelain` and `git log -1 --format=%cr` in the working directory for what has landed.
4. CI: `gh pr checks` or `gh run list --limit 3` when a PR or workflow was mentioned.
5. Timing memory: the project's memory files may hold a typical duration for this job (for example a build or a pipeline run). Use it for the estimate and say so.

Reply in five lines or fewer: state (done, running, stalled, failed, not started), evidence (file and timestamp or process and elapsed time), estimate with its basis, what is blocking if anything, and the one command the user can run to see more. No narrative, no restating the task.
